"""ChemulEvaluator FSM 시나리오 테스트"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.strategy.orderbook import OrderbookTracker
from core.strategy.trade_flow import TradeFlowTracker
from core.strategy.wall_detector import WallDetector, WallState
from core.strategy.chemul_evaluator import ChemulEvaluator, ChemulState


CODE = "005930"


def _build():
    """기본 트래커 세트 + ChemulEvaluator 생성."""
    ob = OrderbookTracker(history_window_sec=120)
    wd = WallDetector(ob, detect_multiplier=5.0,
                      shrink_ratio=0.7, disappear_ratio=0.2,
                      avg_window_sec=60)
    tf = TradeFlowTracker(max_window_sec=120)
    ev = ChemulEvaluator(
        trade_flow=tf, wall_detector=wd, orderbook=ob,
        pullback_pct=-1.5, pullback_window_sec=60,
        strength_short_window=10, strength_long_window=30,
        strength_min=180, state_timeout_sec=60,
    )
    return ob, wd, tf, ev


def _push_ob(ob, wd, ask1_vol, now):
    """호가 push + wall_detector 업데이트."""
    ob.update(CODE, {
        "ask_prices": [101], "ask_volumes": [ask1_vol] + [100]*9,
        "bid_prices": [100], "bid_volumes": [100]*10,
    }, now=now)
    wd.on_orderbook(CODE, now=now)


def _build_baseline(ob, wd, tf, base):
    """t=0~30: 가격 1000, 호가 100, 균형 체결 (baseline)."""
    for i in range(30):
        _push_ob(ob, wd, 100, base + i)
        tf.add_tick(CODE, 1000, "buy", 100, now=base + i)
        tf.add_tick(CODE, 1000, "sell", 100, now=base + i)


# ════════════════════════════════════════
def t1_initial_state():
    print("\n[1] 초기 상태 = WAITING_PULLBACK")
    _, _, _, ev = _build()
    state = ev.evaluate(CODE, now=100.0)
    assert state == ChemulState.WAITING_PULLBACK
    print(f"  ✅ {state.value}")


def t2_pullback_then_wall():
    print("\n[2] 눌림 → WAITING_WALL → WAITING_SHRINK_STRENGTH")
    ob, wd, tf, ev = _build()
    base = 1000.0
    _build_baseline(ob, wd, tf, base)

    # 1분간 -1.5% 눌림
    tf.add_tick(CODE, 985, "sell", 100, now=base + 60)
    state = ev.evaluate(CODE, now=base + 60)
    assert state == ChemulState.WAITING_WALL, f"got {state}"
    print(f"  ✅ 눌림 감지 → {state.value}")

    # 매도벽 등장: ask1=800 (baseline 평균 100의 8배)
    _push_ob(ob, wd, 800, base + 65)
    state = ev.evaluate(CODE, now=base + 65)
    assert state == ChemulState.WAITING_SHRINK_STRENGTH, f"got {state}"
    print(f"  ✅ 벽 등장 → {state.value}")


def t3_full_happy_path():
    print("\n[3] 5단계 happy path → READY_TO_BUY")
    ob, wd, tf, ev = _build()
    base = 1000.0
    _build_baseline(ob, wd, tf, base)

    # 눌림
    tf.add_tick(CODE, 985, "sell", 100, now=base + 60)
    ev.evaluate(CODE, now=base + 60)

    # 벽 등장 (800)
    _push_ob(ob, wd, 800, base + 65)
    ev.evaluate(CODE, now=base + 65)

    # 벽 축소 (500, < 800*0.7=560) + 강한 매수 체결 (10초간 매수 폭주)
    _push_ob(ob, wd, 500, base + 70)
    for i in range(60, 71):  # 11 ticks of buy@100
        tf.add_tick(CODE, 1000, "buy", 100, now=base + i)
    tf.add_tick(CODE, 1000, "sell", 50, now=base + 65)  # 소량 매도

    state = ev.evaluate(CODE, now=base + 70)
    assert state == ChemulState.WAITING_DISAPPEAR, f"got {state}"
    print(f"  ✅ 축소+강도 → {state.value}")

    # 벽 소실 (100, < 800*0.2=160)
    _push_ob(ob, wd, 100, base + 75)
    state = ev.evaluate(CODE, now=base + 75)
    assert state == ChemulState.READY_TO_BUY, f"got {state}"
    print(f"  ✅ 벽 소실 → {state.value}")


def t4_strength_required():
    print("\n[4] 축소돼도 강도 약하면 진행 안 함")
    ob, wd, tf, ev = _build()
    base = 1000.0
    _build_baseline(ob, wd, tf, base)

    # 눌림 → 즉시 evaluate (이 시점에 latest price = 985)
    tf.add_tick(CODE, 985, "sell", 100, now=base + 60)
    ev.evaluate(CODE, now=base + 60)
    assert ev.get_state(CODE) == ChemulState.WAITING_WALL

    # 벽 등장 → 즉시 evaluate
    _push_ob(ob, wd, 800, base + 65)
    ev.evaluate(CODE, now=base + 65)
    assert ev.get_state(CODE) == ChemulState.WAITING_SHRINK_STRENGTH

    # 벽 축소 + 균형 체결 (강한 매수 없음)
    _push_ob(ob, wd, 500, base + 70)
    for i in range(60, 71):
        tf.add_tick(CODE, 1000, "buy", 100, now=base + i)
        tf.add_tick(CODE, 1000, "sell", 100, now=base + i)  # 같은 양 매도

    state = ev.evaluate(CODE, now=base + 70)
    assert state == ChemulState.WAITING_SHRINK_STRENGTH, f"got {state}"
    print(f"  ✅ 강도 부족 → {state.value} 유지")


def t5_no_pullback_no_advance():
    print("\n[5] 눌림 없으면 wall 등장해도 진행 X")
    ob, wd, tf, ev = _build()
    base = 1000.0
    _build_baseline(ob, wd, tf, base)
    # 가격 안 떨어짐 (1000 유지)
    tf.add_tick(CODE, 1000, "buy", 100, now=base + 60)

    state = ev.evaluate(CODE, now=base + 60)
    assert state == ChemulState.WAITING_PULLBACK
    print(f"  ✅ 눌림 없음 → {state.value}")

    # 벽 등장해도 무시
    _push_ob(ob, wd, 800, base + 65)
    state = ev.evaluate(CODE, now=base + 65)
    assert state == ChemulState.WAITING_PULLBACK
    print(f"  ✅ 벽 등장해도 → {state.value} 유지")


def t6_timeout_reset():
    print("\n[6] 타임아웃 → WAITING_PULLBACK 리셋")
    ob, wd, tf, ev = _build()
    base = 1000.0
    _build_baseline(ob, wd, tf, base)

    # 눌림 → WAITING_WALL
    tf.add_tick(CODE, 985, "sell", 100, now=base + 60)
    ev.evaluate(CODE, now=base + 60)
    assert ev.get_state(CODE) == ChemulState.WAITING_WALL

    # 61초 후 (타임아웃 = 60초) 호출 → 리셋
    state = ev.evaluate(CODE, now=base + 60 + 61)
    assert state == ChemulState.WAITING_PULLBACK, f"got {state}"
    print(f"  ✅ 61초 정체 → 리셋 → {state.value}")


def t7_reset_after_ready():
    print("\n[7] reset() 호출 → 초기화")
    ob, wd, tf, ev = _build()
    # 강제로 READY_TO_BUY 상태 만들기
    ev._state[CODE] = {"state": ChemulState.READY_TO_BUY, "ts": 100.0}
    assert ev.get_state(CODE) == ChemulState.READY_TO_BUY

    ev.reset(CODE)
    state = ev.evaluate(CODE, now=200.0)
    assert state == ChemulState.WAITING_PULLBACK
    print(f"  ✅ reset 후 → {state.value}")


if __name__ == "__main__":
    print("=" * 60)
    print("ChemulEvaluator FSM 검증")
    print("=" * 60)
    try:
        t1_initial_state()
        t2_pullback_then_wall()
        t3_full_happy_path()
        t4_strength_required()
        t5_no_pullback_no_advance()
        t6_timeout_reset()
        t7_reset_after_ready()
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