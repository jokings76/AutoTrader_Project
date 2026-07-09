"""4개 마이크로구조 트래커 단위 테스트
   - OrderbookTracker (호가창)
   - TradeFlowTracker (체결 + 가격)
   - WallDetector    (매도벽 FSM)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.strategy.orderbook import OrderbookTracker
from core.strategy.trade_flow import TradeFlowTracker
from core.strategy.wall_detector import WallDetector, WallState


# ════════════════════════════════════════
# OrderbookTracker
# ════════════════════════════════════════
def t1_orderbook_basic():
    print("\n[1] OrderbookTracker — 기본 update/조회")
    ob = OrderbookTracker()
    snap = {
        "ask_prices":  [101, 102, 103, 104, 105, 106, 107, 108, 109, 110],
        "ask_volumes": [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000],
        "bid_prices":  [100, 99, 98, 97, 96, 95, 94, 93, 92, 91],
        "bid_volumes": [150, 250, 350, 450, 550, 650, 750, 850, 950, 1050],
    }
    ob.update("005930", snap, now=1000.0)

    assert ob.get_ask_volume("005930", 1) == 100
    assert ob.get_ask_volume("005930", 5) == 500
    assert ob.get_bid_volume("005930", 1) == 150
    assert ob.get_top_ask("005930") == (101, 100)
    assert ob.get_top_bid("005930") == (100, 150)
    print(f"  ✅ 호가 조회 OK / 매도1={ob.get_top_ask('005930')} / 매수1={ob.get_top_bid('005930')}")


def t2_orderbook_avg_and_window():
    print("\n[2] OrderbookTracker — 시간 윈도우 평균")
    ob = OrderbookTracker(history_window_sec=10)
    base = 1000.0
    for i, vol in enumerate([100, 200, 300, 400, 500]):
        ob.update("005930", {
            "ask_prices": [101], "ask_volumes": [vol],
            "bid_prices": [100], "bid_volumes": [100],
        }, now=base + i * 2)
    avg_all = ob.get_ask_volume_avg("005930", 1)
    assert avg_all == 300, f"전체 평균 기대 300, 실제 {avg_all}"
    print(f"  ✅ 전체 평균 = {avg_all}")
    avg_recent = ob.get_ask_volume_avg("005930", 1, window_sec=5)
    assert avg_recent == 400, f"직전 5초 기대 400, 실제 {avg_recent}"
    print(f"  ✅ 직전 5초 평균 = {avg_recent}")


# ════════════════════════════════════════
# TradeFlowTracker (기본)
# ════════════════════════════════════════
def t3_trade_simple_stats():
    print("\n[3] TradeFlowTracker — 기본 stats")
    tf = TradeFlowTracker(max_window_sec=60)
    base = 1000.0
    for i in range(5):
        tf.add_tick("005930", 1000, "buy", 200, now=base + i)
    for i in range(5):
        tf.add_tick("005930", 1000, "sell", 100, now=base + 10 + i)

    stats = tf.get_stats("005930", window_sec=30, now=base + 20)
    assert stats["count"] == 10
    assert stats["buy_vol"] == 1000
    assert stats["sell_vol"] == 500
    assert stats["strength_simple"] == 200.0
    print(f"  ✅ simple={stats['strength_simple']} / count={stats['count']}")


def t4_trade_weighted_recent_dominates():
    print("\n[4] TradeFlowTracker — 가중 강도는 최근 체결을 더 반영")
    tf = TradeFlowTracker()
    base = 1000.0
    tf.add_tick("005930", 1000, "sell", 1000, now=base)
    tf.add_tick("005930", 1000, "buy",  1000, now=base + 28)

    now = base + 30
    simple = 100  # 1000/1000*100
    weighted = tf.compute_strength("005930", window_sec=30, now=now)
    assert weighted > 200, f"가중 강도가 단순보다 훨씬 커야: {weighted}"
    print(f"  ✅ simple={simple} vs weighted={weighted:.1f} (최근 매수 더 반영됨)")


def t5_trade_rising_strength():
    print("\n[5] TradeFlowTracker — 단기 > 장기 강도 상승 감지")
    tf = TradeFlowTracker()
    base = 1000.0
    for i in range(10):
        tf.add_tick("005930", 1000, "buy", 100, now=base + i)
        tf.add_tick("005930", 1000, "sell", 100, now=base + i)
    for i in range(10):
        tf.add_tick("005930", 1000, "buy", 300, now=base + 20 + i)
        tf.add_tick("005930", 1000, "sell", 100, now=base + 20 + i)

    now = base + 30
    rising = tf.is_strength_rising("005930", short_window=10, long_window=30, now=now)
    assert rising, "단기 강도가 장기보다 강해야 함"
    s10 = tf.compute_strength("005930", 10, now)
    s30 = tf.compute_strength("005930", 30, now)
    print(f"  ✅ 10초 강도={s10:.1f} > 30초 강도={s30:.1f}")


def t6_trade_window_cutoff():
    print("\n[6] TradeFlowTracker — 오래된 틱 자동 제거")
    tf = TradeFlowTracker(max_window_sec=10)
    base = 1000.0
    tf.add_tick("005930", 1000, "buy", 100, now=base)
    tf.add_tick("005930", 1000, "buy", 100, now=base + 5)
    tf.add_tick("005930", 1000, "buy", 100, now=base + 15)
    assert len(tf.ticks["005930"]) == 2
    print(f"  ✅ 윈도우 경계 컷 OK (remaining={len(tf.ticks['005930'])})")


# ════════════════════════════════════════
# WallDetector
# ════════════════════════════════════════
def _push_ob(ob, code, ask1_vol, ts):
    ob.update(code, {
        "ask_prices": [101], "ask_volumes": [ask1_vol] + [100]*9,
        "bid_prices": [100], "bid_volumes": [100]*10,
    }, now=ts)


def t7_wall_detection():
    print("\n[7] WallDetector — IDLE → DETECTED")
    ob = OrderbookTracker(history_window_sec=60)
    wd = WallDetector(ob, detect_multiplier=5.0)
    base = 1000.0
    for i in range(10):
        _push_ob(ob, "005930", 100, base + i)
        st = wd.on_orderbook("005930", now=base + i)
        assert st == WallState.IDLE
    _push_ob(ob, "005930", 700, base + 11)
    st = wd.on_orderbook("005930", now=base + 11)
    assert st == WallState.DETECTED, f"DETECTED 기대, 실제 {st}"
    info = wd.get_info("005930")
    assert info["initial_volume"] == 700
    print(f"  ✅ DETECTED / initial_volume={info['initial_volume']} / level={info['level']}")


def t8_wall_shrink_disappear():
    print("\n[8] WallDetector — DETECTED → SHRINKING → DISAPPEARED")
    ob = OrderbookTracker(history_window_sec=60)
    wd = WallDetector(ob, detect_multiplier=5.0,
                      shrink_ratio=0.7, disappear_ratio=0.2)
    base = 1000.0
    for i in range(10):
        _push_ob(ob, "005930", 100, base + i)
        wd.on_orderbook("005930", now=base + i)
    _push_ob(ob, "005930", 1000, base + 11)
    assert wd.on_orderbook("005930", now=base + 11) == WallState.DETECTED

    _push_ob(ob, "005930", 600, base + 12)
    st = wd.on_orderbook("005930", now=base + 12)
    assert st == WallState.SHRINKING, f"SHRINKING 기대, 실제 {st}"
    print(f"  ✅ SHRINKING (600 ≤ 1000×0.7)")

    _push_ob(ob, "005930", 150, base + 13)
    st = wd.on_orderbook("005930", now=base + 13)
    assert st == WallState.DISAPPEARED, f"DISAPPEARED 기대, 실제 {st}"
    print(f"  ✅ DISAPPEARED (150 ≤ 1000×0.2)")


def t9_wall_grows():
    print("\n[9] WallDetector — 벽이 더 커지면 initial 갱신")
    ob = OrderbookTracker(history_window_sec=60)
    wd = WallDetector(ob, detect_multiplier=5.0)
    base = 1000.0
    for i in range(10):
        _push_ob(ob, "005930", 100, base + i)
        wd.on_orderbook("005930", now=base + i)
    _push_ob(ob, "005930", 700, base + 11)
    wd.on_orderbook("005930", now=base + 11)
    _push_ob(ob, "005930", 1500, base + 12)
    wd.on_orderbook("005930", now=base + 12)
    info = wd.get_info("005930")
    assert info["initial_volume"] == 1500, f"갱신 기대 1500, 실제 {info['initial_volume']}"
    assert info["state"] == WallState.DETECTED
    print(f"  ✅ initial_volume 700 → 1500 갱신")


# ════════════════════════════════════════
# TradeFlowTracker — 가격 추적 (신규)
# ════════════════════════════════════════
def t10_price_latest():
    print("\n[10] TradeFlowTracker — 최신 가격 조회")
    tf = TradeFlowTracker()
    assert tf.get_latest_price("005930") is None
    tf.add_tick("005930", 1000, "buy", 100, now=1000.0)
    tf.add_tick("005930", 1010, "buy", 100, now=1001.0)
    tf.add_tick("005930", 1005, "sell", 100, now=1002.0)
    assert tf.get_latest_price("005930") == 1005
    print(f"  ✅ 최신 가격 = 1005")


def t11_price_around():
    print("\n[11] TradeFlowTracker — N초 전 가격")
    tf = TradeFlowTracker()
    base = 1000.0
    tf.add_tick("005930", 1000, "buy", 100, now=base)
    tf.add_tick("005930", 1020, "buy", 100, now=base + 10)
    tf.add_tick("005930", 1050, "buy", 100, now=base + 30)

    now = base + 30
    # 30초 전 = t=base 시점 → 가격 1000
    assert tf.get_price_around("005930", seconds_ago=30, now=now) == 1000
    # 20초 전 = t=base+10 시점 → 가격 1020
    assert tf.get_price_around("005930", seconds_ago=20, now=now) == 1020
    # 100초 전 = 데이터 없음
    assert tf.get_price_around("005930", seconds_ago=100, now=now) is None
    print(f"  ✅ 30초 전=1000 / 20초 전=1020 / 100초 전=None")


def t12_price_change_pct():
    print("\n[12] TradeFlowTracker — 가격 변화율 (눌림 감지)")
    tf = TradeFlowTracker()
    base = 1000.0
    # 60초 전 1000원, 현재 985원 → -1.5%
    tf.add_tick("005930", 1000, "buy", 100, now=base)
    tf.add_tick("005930", 985, "sell", 100, now=base + 60)

    pct = tf.get_price_change_pct("005930", seconds_ago=60, now=base + 60)
    assert abs(pct - (-1.5)) < 0.001, f"기대 -1.5, 실제 {pct}"
    print(f"  ✅ 1분간 변화 = {pct:.2f}% (눌림 감지 가능)")


if __name__ == "__main__":
    print("=" * 60)
    print("Microstructure Tracker 검증")
    print("=" * 60)
    try:
        t1_orderbook_basic()
        t2_orderbook_avg_and_window()
        t3_trade_simple_stats()
        t4_trade_weighted_recent_dominates()
        t5_trade_rising_strength()
        t6_trade_window_cutoff()
        t7_wall_detection()
        t8_wall_shrink_disappear()
        t9_wall_grows()
        t10_price_latest()
        t11_price_around()
        t12_price_change_pct()
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