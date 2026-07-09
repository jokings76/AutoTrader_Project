"""Phase3Controller 단위 테스트"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from core.strategy.phase3_controller import (
    Phase3Controller, Phase3State, HOLD_DURATION, MAX_TOTAL_WAIT,
)


def _make_ctrl(start: datetime = None):
    now_holder = [start or datetime(2026, 5, 19, 11, 0, 0)]
    ctrl = Phase3Controller(now_func=lambda: now_holder[0])
    ctrl._set_now = lambda t: now_holder.__setitem__(0, t)
    return ctrl


def _trade(code, strength):
    return {"stock_code": code, "strength": strength, "price": 1000, "side": "buy"}


def t1_normal_flow():
    print("\n[1] 정상 흐름 (120→150→180→1분 hold→READY)")
    base = datetime(2026, 5, 19, 11, 0, 0)
    ctrl = _make_ctrl(base)
    ctrl.start_watching("005930")

    ctrl.on_trade(_trade("005930", 125))
    assert ctrl.get_state("005930") == Phase3State.WATCHING_150
    print(f"  ✅ 120 통과 → WATCHING_150")

    ctrl.on_trade(_trade("005930", 155))
    assert ctrl.get_state("005930") == Phase3State.WATCHING_180
    print(f"  ✅ 150 통과 → WATCHING_180")

    ctrl.on_trade(_trade("005930", 185))
    assert ctrl.get_state("005930") == Phase3State.HOLD_180
    print(f"  ✅ 180 도달 → HOLD_180")

    # 1분 경과
    ctrl._set_now(base + timedelta(seconds=61))
    ctrl.on_trade(_trade("005930", 190))
    assert ctrl.get_state("005930") == Phase3State.READY_TO_BUY
    print(f"  ✅ 1분 후 → READY_TO_BUY")


def t2_120_retreat():
    print("\n[2] 120 도달 후 후퇴 → 폐기")
    ctrl = _make_ctrl()
    ctrl.start_watching("005930")
    ctrl.on_trade(_trade("005930", 125))
    ctrl.on_trade(_trade("005930", 115))   # 120 미만
    assert ctrl.get_state("005930") == Phase3State.ABANDONED
    print(f"  ✅ 120 후퇴 (115) → ABANDONED")


def t3_150_retreat():
    print("\n[3] 150 도달 후 후퇴 → 폐기")
    ctrl = _make_ctrl()
    ctrl.start_watching("005930")
    ctrl.on_trade(_trade("005930", 125))
    ctrl.on_trade(_trade("005930", 155))
    ctrl.on_trade(_trade("005930", 145))   # 150 미만
    assert ctrl.get_state("005930") == Phase3State.ABANDONED
    print(f"  ✅ 150 후퇴 (145) → ABANDONED")


def t4_180_retreat():
    print("\n[4] 180 도달 후 후퇴 → 폐기")
    ctrl = _make_ctrl()
    ctrl.start_watching("005930")
    ctrl.on_trade(_trade("005930", 125))
    ctrl.on_trade(_trade("005930", 155))
    ctrl.on_trade(_trade("005930", 185))
    ctrl.on_trade(_trade("005930", 175))   # 180 미만
    assert ctrl.get_state("005930") == Phase3State.ABANDONED
    print(f"  ✅ 180 후퇴 (175) → ABANDONED")


def t5_jump_to_180():
    print("\n[5] 한 번에 180 점프")
    base = datetime(2026, 5, 19, 11, 0, 0)
    ctrl = _make_ctrl(base)
    ctrl.start_watching("005930")
    ctrl.on_trade(_trade("005930", 200))
    assert ctrl.get_state("005930") == Phase3State.HOLD_180
    print(f"  ✅ 120→180 점프 → HOLD_180")

    ctrl._set_now(base + timedelta(seconds=61))
    ctrl.on_trade(_trade("005930", 200))
    assert ctrl.get_state("005930") == Phase3State.READY_TO_BUY
    print(f"  ✅ 1분 후 → READY_TO_BUY")


def t6_total_timeout():
    print("\n[6] 5분 안에 180 못 찍음 → 폐기")
    base = datetime(2026, 5, 19, 11, 0, 0)
    ctrl = _make_ctrl(base)
    ctrl.start_watching("005930")
    ctrl.on_trade(_trade("005930", 125))   # WATCHING_150 정체

    ctrl._set_now(base + MAX_TOTAL_WAIT + timedelta(seconds=1))
    ctrl.on_trade(_trade("005930", 145))   # 150 미달
    assert ctrl.get_state("005930") == Phase3State.ABANDONED
    print(f"  ✅ 5분+ 초과 → ABANDONED")


def t7_tick_ready_without_trade():
    print("\n[7] HOLD 중 체결 없이 1분 경과 → tick으로 READY")
    base = datetime(2026, 5, 19, 11, 0, 0)
    ctrl = _make_ctrl(base)
    ctrl.start_watching("005930")
    ctrl.on_trade(_trade("005930", 200))   # HOLD_180

    # 60초 경과 후 tick만 호출 (추가 체결 없음)
    ctrl._set_now(base + timedelta(seconds=61))
    ready = ctrl.tick()
    assert "005930" in ready
    assert ctrl.get_state("005930") == Phase3State.READY_TO_BUY
    print(f"  ✅ 체결 없이 tick으로 READY 가능")


def t8_is_watching_excludes_done():
    print("\n[8] is_watching - READY/ABANDONED 제외")
    ctrl = _make_ctrl()
    ctrl.start_watching("005930")
    assert ctrl.is_watching("005930")

    ctrl.on_trade(_trade("005930", 115))   # 시작부터 120 미달 → 변화 없음
    assert ctrl.get_state("005930") == Phase3State.WATCHING_120
    assert ctrl.is_watching("005930")
    print(f"  ✅ WATCHING_120 상태 = is_watching True")

    ctrl.on_trade(_trade("005930", 125))
    ctrl.on_trade(_trade("005930", 115))   # 후퇴 → ABANDONED
    assert not ctrl.is_watching("005930")
    print(f"  ✅ ABANDONED 후 is_watching False")


if __name__ == "__main__":
    print("=" * 60)
    print("Phase3Controller 검증")
    print("=" * 60)
    try:
        t1_normal_flow()
        t2_120_retreat()
        t3_150_retreat()
        t4_180_retreat()
        t5_jump_to_180()
        t6_total_timeout()
        t7_tick_ready_without_trade()
        t8_is_watching_excludes_done()
        print("\n" + "=" * 60)
        print("✅ 전체 통과 (8개)")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n❌ 검증 실패: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 예외: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)