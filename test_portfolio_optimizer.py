"""PortfolioOptimizer 단위 테스트"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unittest.mock import MagicMock, patch
from core.strategy.portfolio_optimizer import (
    PortfolioOptimizer, kelly_fraction, compute_atr, _trade_profit,
)


def _candles(opens, highs, lows, closes, vols=None):
    """oldest→newest 입력 → newest-first 반환"""
    n = len(opens)
    if vols is None:
        vols = [1000] * n
    oldest_first = [
        {"open": opens[i], "high": highs[i], "low": lows[i],
         "close": closes[i], "volume": vols[i]}
        for i in range(n)
    ]
    return list(reversed(oldest_first))


def t1_kelly_basic():
    print("\n[1] 켈리 공식")
    # 승률 60%, 평균이익 200, 평균손실 100 → b=2, p=0.6
    # f* = (0.6*2 - 0.4)/2 = 0.4 → cap 0.25 적용
    f = kelly_fraction(0.6, 200, 100, max_fraction=0.25)
    assert abs(f - 0.25) < 1e-6
    print(f"  ✅ 승률60%/손익비2 → cap → f*={f}")

    # 손익비 1, 승률 50% → f*=0
    f = kelly_fraction(0.5, 100, 100)
    assert f == 0.0
    print(f"  ✅ 무위험 베팅 → f*=0")

    # 손실 기대 시나리오
    f = kelly_fraction(0.3, 100, 200)
    assert f == 0.0
    print(f"  ✅ 손실 기대 → f*=0")

    # 정상 분수 켈리
    # 승률 55%, 손익비 1.5 → b=1.5, p=0.55, q=0.45
    # f* = (0.55*1.5 - 0.45)/1.5 = 0.375/1.5 = 0.25 (=cap)
    # 승률 55%, 손익비 1.2 → b=1.2, f* = (0.55*1.2 - 0.45)/1.2 = 0.175
    f = kelly_fraction(0.55, 120, 100)
    assert abs(f - 0.175) < 0.01
    print(f"  ✅ 승률55%/손익비1.2 → f*={f:.3f}")


def t2_atr():
    print("\n[2] ATR 계산")
    candles = _candles(
        opens=[100]*15, highs=[105]*15, lows=[100]*15, closes=[102]*15,
    )
    atr = compute_atr(candles, period=14)
    assert atr is not None
    assert 4.5 <= atr <= 5.5, f"got {atr}"
    print(f"  ✅ 일정 변동성 → ATR={atr:.2f}")

    # 부족 분봉
    short = _candles(opens=[100]*5, highs=[101]*5, lows=[99]*5, closes=[100]*5)
    assert compute_atr(short, period=14) is None
    print(f"  ✅ 분봉 부족 → None")


def t3_trade_profit_extraction():
    print("\n[3] 거래 손익 추출 (다양한 컬럼)")
    # profit_amount 있는 경우
    p = _trade_profit({"profit_amount": 1234})
    assert p == 1234.0
    # 없으면 계산
    p = _trade_profit({"sell_price": 1100, "buy_price": 1000, "buy_quantity": 10})
    assert p == 1000.0
    # 다 없으면 None
    p = _trade_profit({})
    assert p is None
    print(f"  ✅ 3가지 컬럼 패턴 처리")


def t4_volatility_high_low():
    print("\n[4] 변동성 multiplier: 높은 변동성 → 작은 비중, 낮은 변동성 → 큰 비중")
    opt = PortfolioOptimizer(rest_api=MagicMock(), target_volatility=0.02)

    # 변동성 4% 종목 (high-low=4, close=100 → atr≈4%)
    opt.api.get_minute_candles.return_value = _candles(
        opens=[100]*30, highs=[104]*30, lows=[100]*30, closes=[100]*30,
    )
    mult, pct = opt._compute_volatility("AAA")
    assert mult is not None and mult < 1.0
    print(f"  ✅ 변동성 {pct*100:.2f}% → {mult:.2f}x (작아짐)")

    # 변동성 1% 종목
    opt.api.get_minute_candles.return_value = _candles(
        opens=[100]*30, highs=[101]*30, lows=[100]*30, closes=[100]*30,
    )
    mult2, pct2 = opt._compute_volatility("BBB")
    assert mult2 is not None and mult2 > 1.0
    print(f"  ✅ 변동성 {pct2*100:.2f}% → {mult2:.2f}x (커짐)")


def t5_kelly_insufficient_data():
    print("\n[5] 켈리 데이터 부족 → multiplier=1.0")
    opt = PortfolioOptimizer(rest_api=MagicMock(), min_trades_for_kelly=20)

    with patch("core.strategy.portfolio_optimizer.TradeRepository") as mt:
        mt.find_closed_by_substrategy.return_value = [
            {"profit_amount": 100, "buy_quantity": 10}
        ] * 5  # 5건만 (20 미만)
        f = opt._compute_kelly("1A")
        assert f is None
    print(f"  ✅ 5건 < 20건 → Kelly None")


def t6_kelly_with_data():
    print("\n[6] 켈리 데이터 충분 → 계산 동작")
    opt = PortfolioOptimizer(rest_api=MagicMock(), min_trades_for_kelly=10)

    # 승률 60% (12승 8패), 평균이익 200, 평균손실 100
    trades = [{"profit_amount": 200, "buy_quantity": 10}] * 12 + \
             [{"profit_amount": -100, "buy_quantity": 10}] * 8

    with patch("core.strategy.portfolio_optimizer.TradeRepository") as mt:
        mt.find_closed_by_substrategy.return_value = trades
        f = opt._compute_kelly("1A")
        assert f is not None
        assert 0 < f <= opt.kelly_cap
    print(f"  ✅ 20건 데이터 → f*={f:.3f}")


def t7_integration():
    print("\n[7] 통합 계산 (Kelly None + 정상 Vol)")
    opt = PortfolioOptimizer(
        rest_api=MagicMock(),
        base_amount=2_000_000,
    )
    opt.api.get_minute_candles.return_value = _candles(
        opens=[1000]*30, highs=[1010]*30, lows=[1000]*30, closes=[1000]*30,
    )

    with patch("core.strategy.portfolio_optimizer.TradeRepository") as mt:
        mt.find_closed_by_substrategy.return_value = []
        info = opt.calculate_position_amount("005930", "1A")

    assert info["kelly_fraction"] is None
    assert info["kelly_multiplier"] == 1.0
    assert info["amount"] > 0
    assert info["amount"] % 1000 == 0  # 1000원 단위
    print(f"  ✅ amount={info['amount']:,}원, weight={info['final_weight']:.2f}x")
    for r in info["reasons"]:
        print(f"     · {r}")


def t8_clipping():
    print("\n[8] min/max 클리핑")
    opt = PortfolioOptimizer(
        rest_api=MagicMock(),
        min_weight=0.3, max_weight=2.0,
        min_trades_for_kelly=10,
    )
    # 변동성 매우 낮음 (vol_mult가 3.0까지 갈 수 있음)
    opt.api.get_minute_candles.return_value = _candles(
        opens=[1000]*30, highs=[1001]*30, lows=[1000]*30, closes=[1000]*30,
    )
    # 켈리도 매우 좋음 (vol과 곱하면 max 초과)
    trades = [{"profit_amount": 500, "buy_quantity": 10}] * 18 + \
             [{"profit_amount": -100, "buy_quantity": 10}] * 2

    with patch("core.strategy.portfolio_optimizer.TradeRepository") as mt:
        mt.find_closed_by_substrategy.return_value = trades
        info = opt.calculate_position_amount("005930", "1A")

    assert info["final_weight"] <= opt.max_weight + 1e-6
    print(f"  ✅ max 클리핑 동작: final_weight={info['final_weight']:.2f}")


if __name__ == "__main__":
    print("=" * 60)
    print("PortfolioOptimizer 검증")
    print("=" * 60)
    try:
        t1_kelly_basic()
        t2_atr()
        t3_trade_profit_extraction()
        t4_volatility_high_low()
        t5_kelly_insufficient_data()
        t6_kelly_with_data()
        t7_integration()
        t8_clipping()
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