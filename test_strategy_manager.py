"""StrategyManager 단위 테스트 (API/주문 모킹) — 5/19 패치 반영"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, time, timedelta
from unittest.mock import MagicMock, patch

from core.strategy_manager import (
    StrategyManager, PHASE1_START, PHASE1_END, PHASE2_END,
    POSITION_AMOUNT, MAX_HOLDINGS, MAX_SELL_FAIL, REBUY_COOLDOWN, RESTART_WARMUP,
    TRAILING_ARM_RATE, TRAILING_DROP, TRAILING_MIN_PROFIT,
)
from core.strategy.chemul_evaluator import ChemulState


def _make_sm(now_dt: datetime, optimizer=None):
    """DB 복원 우회한 StrategyManager 생성"""
    sm = StrategyManager.__new__(StrategyManager)
    sm.api = MagicMock()
    sm.order_manager = MagicMock()
    sm.phase1b = None
    sm.phase3 = None
    sm.optimizer = optimizer
    sm._now = lambda: now_dt
    sm.holdings = {}
    sm.watch_list_today = set()
    sm.pending = set()
    sm._stock_names = {}
    sm._sell_fail_count = {}
    sm._sell_blocked = set()
    sm._sold_at = {}
    return sm


def _make_sm_with_phase1b(now_dt: datetime, phase1b=None, optimizer=None):
    sm = StrategyManager.__new__(StrategyManager)
    sm.api = MagicMock()
    sm.order_manager = MagicMock()
    sm.phase1b = phase1b or MagicMock()
    sm.phase3 = None
    sm.optimizer = optimizer
    sm._now = lambda: now_dt
    sm.holdings = {}
    sm.watch_list_today = set()
    sm.pending = set()
    sm._stock_names = {}
    sm._sell_fail_count = {}
    sm._sell_blocked = set()
    sm._sold_at = {}
    return sm

def _build_candles(open_price, closes, volumes):
    oldest_first = [{"open": open_price, "close": open_price,
                     "high": open_price, "low": open_price,
                     "volume": volumes[0]}]
    prev_close = open_price
    for c, v in zip(closes, volumes[1:]):
        oldest_first.append({
            "open": prev_close, "close": c,
            "high": max(prev_close, c), "low": min(prev_close, c),
            "volume": v,
        })
        prev_close = c
    return list(reversed(oldest_first))


def _make_position(buy_price=1000, buy_time=None, warmup_until=None,
                   sub_strategy="1A", quantity=10, highest=None):
    return {
        "trade_id": 99,
        "buy_price": buy_price,
        "buy_quantity": quantity,
        "buy_time": buy_time or datetime.now(),
        "stock_name": "테스트",
        "strategy_phase": 1,
        "sub_strategy": sub_strategy,
        "highest_price": highest or buy_price,
        "trailing_armed": False,
        "position_weight": 1.0,
        "warmup_until": warmup_until,
    }


def t1_phase_detection():
    print("\n[1] Phase 시간대 판별")
    cases = [
        (time(8, 59),  None),  # 장 시작 전
        (time(9, 0),   1),     # Phase 1A 시작
        (time(9, 19),  1),     # Phase 1A
        (time(9, 20),  None),  # ★ 09:20~09:21 공백 (의도적)
        (time(9, 21),  2),     # ★ Phase 2 시작
        (time(10, 39), 2),     # Phase 2
        (time(10, 40), None),  # ★ 10:40~10:41 공백 (의도적)
        (time(10, 41), 3),     # ★ Phase 3 시작
        (time(14, 59), 3),     # ★ Phase 3
        (time(15, 0),  None),  # ★ Phase 3 종료
        (time(15, 30), None),  # 장 마감 후
    ]
    for t, expected in cases:
        sm = _make_sm(datetime.combine(datetime.today(), t))
        assert sm.get_current_phase() == expected, f"{t} expected {expected}, got {sm.get_current_phase()}"
        print(f"  ✅ {t} → {expected}")
   


def t2_volume_ratio():
    print("\n[2] 거래량 비율")
    sm = _make_sm(datetime.now())
    candles = _build_candles(100, [100]*6, [1000]*6 + [2000])
    ratio = sm._volume_ratio(candles)
    assert abs(ratio - 2.0) < 0.01
    print(f"  ✅ {ratio:.2f}배")


def t3_phase1_pass():
    print("\n[3] Phase 1 통과")
    sm = _make_sm(datetime.now())
    candles = _build_candles(100, [103, 105, 105, 106, 104, 105],
                             [1000]*6 + [2000])
    ok, info = sm.evaluate_phase1(candles)
    assert ok
    print(f"  ✅ 시초가+{info['surge_rate']*100:.2f}% / MA5={info['ma5']:.1f}")


def t4_phase1_fail_modes():
    print("\n[4] Phase 1 거부")
    sm = _make_sm(datetime.now())
    c = _build_candles(100, [101, 102, 103, 102, 103, 103], [1000]*6 + [2000])
    ok, info = sm.evaluate_phase1(c)
    assert not ok and "시초가" in info["reason"]
    print(f"  ✅ 시초가: {info['reason']}")

    c = _build_candles(100, [100]*5 + [110], [1000]*6 + [2000])
    ok, info = sm.evaluate_phase1(c)
    assert not ok and "MA" in info["reason"]
    print(f"  ✅ MA: {info['reason']}")

    c = _build_candles(100, [104, 105, 104, 105, 106, 105], [1000]*6 + [2000])
    ok, info = sm.evaluate_phase1(c)
    assert not ok and "음봉" in info["reason"]
    print(f"  ✅ 음봉: {info['reason']}")

    c = _build_candles(100, [103, 105, 105, 106, 104, 105], [1000]*6 + [1100])
    ok, info = sm.evaluate_phase1(c)
    assert not ok and "거래량" in info["reason"]
    print(f"  ✅ 거래량: {info['reason']}")


def t5_capacity():
    print("\n[5] 보유 한도")
    sm = _make_sm(datetime.now())
    for i in range(MAX_HOLDINGS):
        sm.holdings[f"00000{i}"] = {}
    assert not sm.can_buy_more()
    print(f"  ✅ {MAX_HOLDINGS}종목 시 차단")


def t6_exit_signals():
    """5/19 패치: 트레일링 무장 +1.5%, drop -1.2%, min_profit +0.5%"""
    print("\n[6] 하이브리드 청산 (5/19 패치 반영)")
    base = datetime.now()
    def fresh():
        sm = _make_sm(base)
        sm.holdings["005930"] = _make_position(buy_price=1000, buy_time=base)
        sm._execute_sell = MagicMock()
        return sm

    # 손절 -1.5% → 985원 이하
    sm = fresh(); sm.on_price_update("005930", 984)
    assert sm._execute_sell.called and "손절" in sm._execute_sell.call_args[0][2]
    print(f"  ✅ 손절 (984원, -1.6%)")

    # 익절 캡 +2.5% → 1025원 이상
    sm = fresh(); sm.on_price_update("005930", 1026)
    assert sm._execute_sell.called and "익절 캡" in sm._execute_sell.call_args[0][2]
    print(f"  ✅ 익절 캡 (1026원, +2.6%)")

    # 트레일링 무장 (+1.5%) but 청산 X (drop 미달)
    sm = fresh(); sm.on_price_update("005930", 1015)  # +1.5%
    assert not sm._execute_sell.called
    assert sm.holdings["005930"]["trailing_armed"] is True
    print(f"  ✅ 트레일링 무장만 (1015원, +1.5%)")

    # 무장 미달 (+1.2%) — 5/19 패치로 +1.5% 미만은 무장 X
    sm = fresh(); sm.on_price_update("005930", 1012)
    assert not sm._execute_sell.called
    assert sm.holdings["005930"]["trailing_armed"] is False
    print(f"  ✅ 무장 미달 (1012원, +1.2% < +1.5%)")

    # 트레일링 청산: +2% 무장 → -1.2% drop & rate +0.5% 이상
    sm = fresh()
    sm.on_price_update("005930", 1020)   # +2% 무장, 고점=1020
    sm._execute_sell.reset_mock()
    sm.on_price_update("005930", 1006)   # drop=1.37%, rate=+0.6%
    assert sm._execute_sell.called and "트레일링" in sm._execute_sell.call_args[0][2]
    print(f"  ✅ 트레일링 청산 (1020→1006, drop 1.37% + rate +0.6%)")

    # ★ 신규: min_profit 미달 시 트레일링 청산 차단
    sm = fresh()
    sm.on_price_update("005930", 1020)   # +2% 무장
    sm._execute_sell.reset_mock()
    sm.on_price_update("005930", 1003)   # drop=1.67%, rate=+0.3% < +0.5%
    assert not sm._execute_sell.called
    print(f"  ✅ 트레일링 차단 (rate +0.3% < min_profit +0.5%)")

    # 시간정리
    sm = fresh(); sm._now = lambda: base + timedelta(minutes=31)
    sm.on_price_update("005930", 1005)
    assert sm._execute_sell.called and "시간정리" in sm._execute_sell.call_args[0][2]
    print(f"  ✅ 시간정리 (31분 경과)")


def t7_buy_flow_integration():
    print("\n[7] 매수 플로우 (optimizer 없음)")
    sm = _make_sm(datetime.combine(datetime.today(), time(9, 5)))
    sm.api.get_minute_candles.return_value = _build_candles(
        100, [103, 105, 105, 106, 104, 105], [1000]*6 + [2000]
    )
    sm.order_manager.buy.return_value = {"success": True}

    with patch("core.strategy_manager.TradeRepository") as mt, \
         patch("core.strategy_manager.WatchListRepository") as mw, \
         patch("core.strategy_manager.SystemEventRepository"):
        mt.insert_buy.return_value = 123
        mw.find_by_date.return_value = []
        sm.on_condition_hit("005930", "삼성전자")

    assert "005930" in sm.holdings
    assert sm.holdings["005930"]["sub_strategy"] == "1A"
    expected_qty = int(POSITION_AMOUNT // 105)
    assert sm.order_manager.buy.call_args[0][1] == expected_qty
    print(f"  ✅ POSITION_AMOUNT → {expected_qty}주")


def t8_phase1a_slots():
    print("\n[8] Phase 1A 동시 보유 3슬롯")
    from core.strategy_manager import PHASE1A_MAX_SLOTS
    sm = _make_sm_with_phase1b(datetime.combine(datetime.today(), time(9, 5)))
    for i in range(PHASE1A_MAX_SLOTS):
        sm.holdings[f"00000{i}"] = {"sub_strategy": "1A"}
    assert not sm.can_buy_phase1a()
    print(f"  ✅ {PHASE1A_MAX_SLOTS}슬롯 차단")
    del sm.holdings["000000"]
    assert sm.can_buy_phase1a()
    print(f"  ✅ 청산 후 부활")
    sm._now = lambda: datetime.combine(datetime.today(), time(9, 25))
    assert not sm.can_buy_phase1a()
    print(f"  ✅ 09:20 이후 차단")


def t9_phase1b_slots():
    print("\n[9] Phase 1B 동시 보유 2슬롯")
    from core.strategy_manager import PHASE1B_MAX_SLOTS
    sm = _make_sm_with_phase1b(datetime.combine(datetime.today(), time(9, 30)))
    for i in range(PHASE1B_MAX_SLOTS):
        sm.holdings[f"00000{i}"] = {"sub_strategy": "1B"}
    assert not sm.can_buy_phase1b()
    print(f"  ✅ {PHASE1B_MAX_SLOTS}슬롯 차단")
    del sm.holdings["000000"]
    assert sm.can_buy_phase1b()
    print(f"  ✅ 부활")
    sm._now = lambda: datetime.combine(datetime.today(), time(10, 45))
    assert not sm.can_buy_phase1b()
    print(f"  ✅ 10:40 이후 차단")


def t10_phase1b_buy_via_callback():
    print("\n[10] Phase 1B READY → 매수")
    sm = _make_sm_with_phase1b(datetime.combine(datetime.today(), time(9, 10)))
    sm.phase1b.is_watching = MagicMock(return_value=True)
    sm.phase1b.on_trade = MagicMock(return_value=ChemulState.READY_TO_BUY)
    sm.phase1b.trade_flow.get_latest_price = MagicMock(return_value=1000)
    sm._stock_names["005930"] = "삼성전자"
    sm.order_manager.buy.return_value = {"success": True}

    with patch("core.strategy_manager.TradeRepository") as mt, \
         patch("core.strategy_manager.WatchListRepository") as mw, \
         patch("core.strategy_manager.SystemEventRepository"):
        mt.insert_buy.return_value = 555
        mw.find_by_date.return_value = []
        sm.on_trade({"stock_code": "005930", "price": 1000,
                     "side": "buy", "volume": 100})

    assert "005930" in sm.holdings
    assert sm.holdings["005930"]["sub_strategy"] == "1B"
    sm.phase1b.stop_watching.assert_called_with("005930")
    print(f"  ✅ READY → 매수 + stop_watching")


def t11_phase1a_buy_skips_phase1b_watch():
    print("\n[11] Phase 1A 성공 시 1B 감시 X")
    sm = _make_sm_with_phase1b(datetime.combine(datetime.today(), time(9, 5)))
    sm.api.get_minute_candles.return_value = _build_candles(
        100, [103, 105, 105, 106, 104, 105], [1000]*6 + [2000]
    )
    sm.order_manager.buy.return_value = {"success": True}
    sm.phase1b.is_watching = MagicMock(return_value=False)

    with patch("core.strategy_manager.TradeRepository") as mt, \
         patch("core.strategy_manager.WatchListRepository") as mw, \
         patch("core.strategy_manager.SystemEventRepository"):
        mt.insert_buy.return_value = 100
        mw.find_by_date.return_value = []
        sm.on_condition_hit("005930", "삼성전자")

    assert "005930" in sm.holdings
    sm.phase1b.start_watching.assert_not_called()
    print(f"  ✅ 1A 성공 → 1B 감시 X")


def t12_phase1b_watch_after_phase1a_fail():
    print("\n[12] Phase 1A 실패 시 1B 감시 O")
    sm = _make_sm_with_phase1b(datetime.combine(datetime.today(), time(9, 5)))
    sm.api.get_minute_candles.return_value = _build_candles(
        100, [101, 102, 103, 102, 103, 103], [1000]*6 + [2000]
    )
    sm.phase1b.is_watching = MagicMock(return_value=False)

    with patch("core.strategy_manager.WatchListRepository") as mw, \
         patch("core.strategy_manager.SystemEventRepository"):
        mw.find_by_date.return_value = []
        sm.on_condition_hit("005930", "삼성전자")

    assert "005930" not in sm.holdings
    sm.phase1b.start_watching.assert_called_with("005930")
    print(f"  ✅ 1A 실패 → 1B 감시 O")


def t13_optimizer_integration():
    print("\n[13] PortfolioOptimizer 통합")
    optimizer = MagicMock()
    optimizer.calculate_position_amount.return_value = {
        "amount": 3_000_000, "base": 2_000_000,
        "kelly_fraction": None, "kelly_multiplier": 1.0,
        "vol_multiplier": 1.5, "atr_pct": 0.013,
        "final_weight": 1.5, "reasons": ["test"],
    }
    sm = _make_sm(datetime.combine(datetime.today(), time(9, 5)), optimizer=optimizer)
    sm.api.get_minute_candles.return_value = _build_candles(
        100, [103, 105, 105, 106, 104, 105], [1000]*6 + [2000]
    )
    sm.order_manager.buy.return_value = {"success": True}

    with patch("core.strategy_manager.TradeRepository") as mt, \
         patch("core.strategy_manager.WatchListRepository") as mw, \
         patch("core.strategy_manager.SystemEventRepository"):
        mt.insert_buy.return_value = 200
        mw.find_by_date.return_value = []
        sm.on_condition_hit("005930", "삼성전자")

    expected_qty = int(3_000_000 // 105)
    assert sm.order_manager.buy.call_args[0][1] == expected_qty
    assert sm.holdings["005930"]["position_weight"] == 1.5
    print(f"  ✅ 동적 금액 3M → {expected_qty}주")


def t14_optimizer_exception_fallback():
    print("\n[14] Optimizer 예외 → fallback")
    optimizer = MagicMock()
    optimizer.calculate_position_amount.side_effect = Exception("DB down")
    sm = _make_sm(datetime.combine(datetime.today(), time(9, 5)), optimizer=optimizer)
    sm.api.get_minute_candles.return_value = _build_candles(
        100, [103, 105, 105, 106, 104, 105], [1000]*6 + [2000]
    )
    sm.order_manager.buy.return_value = {"success": True}

    with patch("core.strategy_manager.TradeRepository") as mt, \
         patch("core.strategy_manager.WatchListRepository") as mw, \
         patch("core.strategy_manager.SystemEventRepository"):
        mt.insert_buy.return_value = 201
        mw.find_by_date.return_value = []
        sm.on_condition_hit("005930", "삼성전자")

    expected_qty = int(POSITION_AMOUNT // 105)
    assert sm.order_manager.buy.call_args[0][1] == expected_qty
    print(f"  ✅ 예외 시 기본 {POSITION_AMOUNT:,}원")


# ═════════════════════════════════════════════
# 5/19 신규 동작 검증
# ═════════════════════════════════════════════
def t15_sell_failure_blocking():
    """매도 3회 실패 시 holdings 제거 + sell_blocked"""
    print("\n[15] 매도 실패 처리 (3회 실패 → 차단)")
    base = datetime.now()
    sm = _make_sm(base)
    sm.holdings["005930"] = _make_position(buy_price=1000, buy_time=base)
    sm.order_manager.sell.return_value = {"success": False, "error": "잔고 부족"}

    with patch("core.strategy_manager.TradeRepository"), \
         patch("core.strategy_manager.SystemEventRepository"):
        # 1회 실패
        sm._execute_sell("005930", 985, "테스트")
        assert sm._sell_fail_count["005930"] == 1
        assert "005930" in sm.holdings
        assert "005930" not in sm._sell_blocked
        print(f"  ✅ 1회 실패: 카운트=1, holdings 유지")

        # 2회 실패
        sm._execute_sell("005930", 985, "테스트")
        assert sm._sell_fail_count["005930"] == 2
        assert "005930" in sm.holdings
        print(f"  ✅ 2회 실패: 카운트=2, holdings 유지")

        # 3회 실패 → 차단 + 제거
        sm._execute_sell("005930", 985, "테스트")
        assert sm._sell_fail_count["005930"] == 3
        assert "005930" in sm._sell_blocked
        assert "005930" not in sm.holdings
        print(f"  ✅ 3회 실패: 차단 + holdings 제거")

    # 차단 종목은 더 이상 청산 시도 안 함
    sm.order_manager.sell.reset_mock()
    sm._execute_sell("005930", 985, "테스트")
    sm.order_manager.sell.assert_not_called()
    print(f"  ✅ 차단 후 추가 매도 시도 없음")


def t16_rebuy_cooldown():
    """매도 후 10분 동안 같은 종목 재매수 차단"""
    print("\n[16] 재매수 쿨다운")
    base = datetime.combine(datetime.today(), time(9, 5))
    sm = _make_sm_with_phase1b(base)
    sm.api.get_minute_candles.return_value = _build_candles(
        100, [103, 105, 105, 106, 104, 105], [1000]*6 + [2000]
    )

    # 매도 직후 마킹
    sm._sold_at["005930"] = base
    sm.phase1b.is_watching = MagicMock(return_value=False)

    with patch("core.strategy_manager.TradeRepository") as mt, \
         patch("core.strategy_manager.WatchListRepository") as mw, \
         patch("core.strategy_manager.SystemEventRepository"):
        mw.find_by_date.return_value = []
        sm.on_condition_hit("005930", "삼성전자")

    # 쿨다운 잔여 동안은 매수 X
    assert "005930" not in sm.holdings
    sm.order_manager.buy.assert_not_called()
    print(f"  ✅ 매도 직후: 매수 차단")

    # 쿨다운 종료 후 (10분+1초 경과)
    sm._now = lambda: base + REBUY_COOLDOWN + timedelta(seconds=1)
    sm.order_manager.buy.return_value = {"success": True}
    with patch("core.strategy_manager.TradeRepository") as mt, \
         patch("core.strategy_manager.WatchListRepository") as mw, \
         patch("core.strategy_manager.SystemEventRepository"):
        mt.insert_buy.return_value = 999
        mw.find_by_date.return_value = []
        sm.on_condition_hit("005930", "삼성전자")
    assert "005930" in sm.holdings
    print(f"  ✅ 10분 경과 후: 매수 OK")


def t17_restart_warmup():
    """재시작 후 30초 동안 청산 평가 보류"""
    print("\n[17] 재시작 워밍업")
    base = datetime.now()
    sm = _make_sm(base)
    # 워밍업 중인 포지션 (DB 복원 직후 시뮬레이션)
    sm.holdings["005930"] = _make_position(
        buy_price=1000, buy_time=base - timedelta(minutes=5),
        warmup_until=base + RESTART_WARMUP,
    )
    sm._execute_sell = MagicMock()

    # 워밍업 중: 어떤 가격도 청산 발동 X
    sm.on_price_update("005930", 984)   # 손절 라인
    assert not sm._execute_sell.called
    print(f"  ✅ 워밍업 중: 손절 라인(-1.6%)도 청산 안 함")
    assert sm.holdings["005930"]["highest_price"] == 1000  # 984 < 1000
    
    sm.on_price_update("005930", 1050)  # 익절 캡 초과
    assert not sm._execute_sell.called
    assert sm.holdings["005930"]["highest_price"] == 1050  # 갱신은 됨
    print(f"  ✅ 워밍업 중: 익절 캡(+5%)도 청산 안 함, 고점만 갱신")

    # 워밍업 종료 후
    sm._now = lambda: base + RESTART_WARMUP + timedelta(seconds=1)
    sm.on_price_update("005930", 984)
    assert sm._execute_sell.called
    print(f"  ✅ 30초 경과 후: 손절 정상 동작")


def t18_buy_clears_cooldown():
    """매수 성공 시 이전 _sold_at 클리어"""
    print("\n[18] 매수 성공 → 쿨다운 클리어")
    base = datetime.combine(datetime.today(), time(9, 5))
    # 11분 전 매도된 종목 (쿨다운 종료)
    sm = _make_sm(base)
    sm._sold_at["005930"] = base - timedelta(minutes=11)
    sm.api.get_minute_candles.return_value = _build_candles(
        100, [103, 105, 105, 106, 104, 105], [1000]*6 + [2000]
    )
    sm.order_manager.buy.return_value = {"success": True}

    with patch("core.strategy_manager.TradeRepository") as mt, \
         patch("core.strategy_manager.WatchListRepository") as mw, \
         patch("core.strategy_manager.SystemEventRepository"):
        mt.insert_buy.return_value = 777
        mw.find_by_date.return_value = []
        sm.on_condition_hit("005930", "삼성전자")

    assert "005930" in sm.holdings
    # 매수 성공 시 _sold_at에서 제거됨
    assert "005930" not in sm._sold_at
    print(f"  ✅ 매수 성공 후 _sold_at 클리어")


if __name__ == "__main__":
    print("=" * 60)
    print("StrategyManager 검증 (5/19 패치 반영)")
    print("=" * 60)
    try:
        t1_phase_detection()
        t2_volume_ratio()
        t3_phase1_pass()
        t4_phase1_fail_modes()
        t5_capacity()
        t6_exit_signals()
        t7_buy_flow_integration()
        t8_phase1a_slots()
        t9_phase1b_slots()
        t10_phase1b_buy_via_callback()
        t11_phase1a_buy_skips_phase1b_watch()
        t12_phase1b_watch_after_phase1a_fail()
        t13_optimizer_integration()
        t14_optimizer_exception_fallback()
        t15_sell_failure_blocking()
        t16_rebuy_cooldown()
        t17_restart_warmup()
        t18_buy_clears_cooldown()
        print("\n" + "=" * 60)
        print("✅ 전체 통과 (18개)")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n❌ 검증 실패: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 예외: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)