"""Phase1BController 통합 테스트 — wrapper + WS 콜백 인터페이스 검증"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.phase1b_controller import Phase1BController
from core.strategy.chemul_evaluator import ChemulState


CODE = "005930"


def _ob_msg(ask1_vol, code=CODE):
    """on_orderbook용 파싱된 호가 dict 생성."""
    return {
        "stock_code": code,
        "ask_prices": [101] * 10,
        "ask_volumes": [ask1_vol] + [100] * 9,
        "bid_prices": [100] * 10,
        "bid_volumes": [100] * 10,
    }


def _tr_msg(price, side, volume, code=CODE):
    """on_trade용 파싱된 체결 dict 생성."""
    return {"stock_code": code, "price": price, "side": side, "volume": volume}


def _push_baseline(ctrl, base):
    """t=0~29: 가격 1000, ask1=100, 균형 체결 (baseline)."""
    for i in range(30):
        ctrl.on_orderbook(_ob_msg(100), now=base + i)
        ctrl.on_trade(_tr_msg(1000, "buy", 100), now=base + i)
        ctrl.on_trade(_tr_msg(1000, "sell", 100), now=base + i)


# ════════════════════════════════════════
def t1_watch_lifecycle():
    print("\n[1] start_watching / stop_watching / is_watching")
    ctrl = Phase1BController()
    assert not ctrl.is_watching(CODE)

    ctrl.start_watching(CODE)
    assert ctrl.is_watching(CODE)
    print("  ✅ start_watching → True")

    ctrl.stop_watching(CODE)
    assert not ctrl.is_watching(CODE)
    print("  ✅ stop_watching → False")


def t2_untracked_returns_none():
    print("\n[2] 감시 안 한 종목 콜백 → None")
    ctrl = Phase1BController()
    r1 = ctrl.on_trade(_tr_msg(1000, "buy", 100), now=100.0)
    r2 = ctrl.on_orderbook(_ob_msg(500), now=100.0)
    assert r1 is None and r2 is None
    print("  ✅ on_trade=None, on_orderbook=None")


def t3_initial_state():
    print("\n[3] 감시 시작 직후 첫 콜백 → WAITING_PULLBACK")
    ctrl = Phase1BController()
    ctrl.start_watching(CODE)
    state = ctrl.on_trade(_tr_msg(1000, "buy", 100), now=100.0)
    assert state == ChemulState.WAITING_PULLBACK
    print(f"  ✅ {state.value}")


def t4_full_happy_path():
    print("\n[4] 전체 happy path → READY_TO_BUY (WS 콜백 통합)")
    ctrl = Phase1BController()
    ctrl.start_watching(CODE)
    base = 1000.0

    # baseline
    _push_baseline(ctrl, base)

    # 눌림 (985 sell at t=60)
    state = ctrl.on_trade(_tr_msg(985, "sell", 100), now=base + 60)
    assert state == ChemulState.WAITING_WALL, f"got {state}"
    print(f"  ✅ 눌림 → {state.value}")

    # 매도벽 등장 (ask1=800 at t=65)
    state = ctrl.on_orderbook(_ob_msg(800), now=base + 65)
    assert state == ChemulState.WAITING_SHRINK_STRENGTH, f"got {state}"
    print(f"  ✅ 벽 등장 → {state.value}")

    # 강한 매수 폭주 (t=60~70: 매수 11틱 + 소량 매도 1틱)
    for i in range(60, 71):
        ctrl.on_trade(_tr_msg(1000, "buy", 100), now=base + i)
    ctrl.on_trade(_tr_msg(1000, "sell", 50), now=base + 65)

    # 벽 축소 (ask1=500 at t=70) → SHRINKING + 강도↑ → WAITING_DISAPPEAR
    state = ctrl.on_orderbook(_ob_msg(500), now=base + 70)
    assert state == ChemulState.WAITING_DISAPPEAR, f"got {state}"
    print(f"  ✅ 축소+강도 → {state.value}")

    # 벽 소실 (ask1=100 at t=75) → DISAPPEARED → READY_TO_BUY
    state = ctrl.on_orderbook(_ob_msg(100), now=base + 75)
    assert state == ChemulState.READY_TO_BUY, f"got {state}"
    print(f"  ✅ 벽 소실 → {state.value}")


def t5_stop_watching_clears_memory():
    print("\n[5] stop_watching 시 내부 트래커 모두 정리")
    ctrl = Phase1BController()
    ctrl.start_watching(CODE)
    base = 1000.0

    # 데이터 좀 쌓기
    for i in range(5):
        ctrl.on_trade(_tr_msg(1000, "buy", 100), now=base + i)
        ctrl.on_orderbook(_ob_msg(500), now=base + i)

    # 트래커에 데이터 있는지 확인
    assert CODE in ctrl.trade_flow.ticks
    assert CODE in ctrl.orderbook.snapshots

    ctrl.stop_watching(CODE)

    # 모두 비워졌는지
    assert CODE not in ctrl.trade_flow.ticks
    assert CODE not in ctrl.orderbook.snapshots
    assert CODE not in ctrl.orderbook.history
    assert CODE not in ctrl.evaluator._state
    assert ctrl.wall_detector.get_info(CODE) is None
    print("  ✅ 4개 트래커 메모리 모두 cleanup")


def t6_multi_stock_watching():
    print("\n[6] 여러 종목 동시 감시 가능")
    ctrl = Phase1BController()
    codes = ["005930", "000660", "035720"]
    for c in codes:
        ctrl.start_watching(c)
    assert all(ctrl.is_watching(c) for c in codes)

    # 각 종목별로 독립적인 상태 추적
    for c in codes:
        state = ctrl.on_trade(_tr_msg(1000, "buy", 100, code=c), now=100.0)
        assert state == ChemulState.WAITING_PULLBACK

    # 한 종목만 stop
    ctrl.stop_watching(codes[0])
    assert not ctrl.is_watching(codes[0])
    assert ctrl.is_watching(codes[1])
    assert ctrl.is_watching(codes[2])
    print(f"  ✅ 3종목 독립 추적 / 1종목만 선택적 stop")


def t7_callback_with_unknown_stock_in_msg():
    print("\n[7] msg에 다른 종목코드 들어와도 감시 종목 아니면 무시")
    ctrl = Phase1BController()
    ctrl.start_watching(CODE)  # 005930만 감시

    # 다른 종목 데이터 들어옴
    result = ctrl.on_trade(_tr_msg(1000, "buy", 100, code="999999"), now=100.0)
    assert result is None

    # 감시 종목 데이터는 정상 처리
    result = ctrl.on_trade(_tr_msg(1000, "buy", 100), now=100.0)
    assert result == ChemulState.WAITING_PULLBACK
    print("  ✅ 비감시 종목 무시 / 감시 종목 정상 처리")


if __name__ == "__main__":
    print("=" * 60)
    print("Phase1BController 검증")
    print("=" * 60)
    try:
        t1_watch_lifecycle()
        t2_untracked_returns_none()
        t3_initial_state()
        t4_full_happy_path()
        t5_stop_watching_clears_memory()
        t6_multi_stock_watching()
        t7_callback_with_unknown_stock_in_msg()
        print("\n" + "=" * 60)
        print("✅ 전체 통과")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n❌ 검증 실패: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 예외: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)