"""
포트폴리오 최적화 — 진입 비중 동적 조절

전략:
  1. 켈리 공식: 과거 거래 (sub_strategy 별) 승률 × 손익비 → 베팅 분수
  2. 변동성 타겟팅: ATR/현재가 → 목표 변동성 대비 비중 조정

최종 금액:
  base_amount × kelly_multiplier × volatility_multiplier  (클리핑)

켈리 계수는 거래 데이터가 부족할 때 (< min_trades) 1.0x로 대체.
즉 봇 초기엔 변동성 타겟팅만 동작, 데이터 쌓이면 자동 활성화.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from db import TradeRepository
except Exception:
    TradeRepository = None


DEFAULT_BASE_AMOUNT = 2_000_000


# ─────────────────────────────────────────────
# 순수 함수: 켈리 분수
# ─────────────────────────────────────────────
def kelly_fraction(
    win_rate: float, avg_win: float, avg_loss: float,
    max_fraction: float = 0.25,
) -> float:
    """켈리 공식 f* = (p*b - q) / b 를 [0, max_fraction]으로 클리핑.

    Args:
        win_rate:  승률 0~1
        avg_win:   평균 수익 (양수)
        avg_loss:  평균 손실 (양수, 절대값)
        max_fraction: 보통 1/4 켈리(0.25) 사용
    """
    if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1 or avg_win <= 0:
        return 0.0
    b = avg_win / avg_loss
    q = 1.0 - win_rate
    f = (win_rate * b - q) / b
    return max(0.0, min(f, max_fraction))


# ─────────────────────────────────────────────
# 순수 함수: ATR
# ─────────────────────────────────────────────
def compute_atr(candles: list[dict], period: int = 14) -> Optional[float]:
    """ATR (newest-first candles 가정).
    TR = max(high-low, |high-prev_close|, |low-prev_close|)
    ATR = 최근 period개 TR의 단순평균
    """
    if not candles or len(candles) < period + 1:
        return None
    trs = []
    for i in range(period):
        cur = candles[i]
        prev = candles[i + 1]
        tr = max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


# ─────────────────────────────────────────────
# 거래 손익 추출 (컬럼 이름이 다양해도 대응)
# ─────────────────────────────────────────────
def _trade_profit(t: dict) -> Optional[float]:
    """trades row에서 손익 추출. profit_amount 컬럼 있으면 우선, 없으면 계산."""
    if t.get("profit_amount") is not None:
        try:
            return float(t["profit_amount"])
        except (TypeError, ValueError):
            pass
    sell = t.get("sell_price")
    buy = t.get("buy_price")
    qty = t.get("buy_quantity") or t.get("sell_quantity")
    if sell is None or buy is None or qty is None:
        return None
    try:
        return (float(sell) - float(buy)) * int(qty)
    except (TypeError, ValueError):
        return None


# ═════════════════════════════════════════════
# PortfolioOptimizer
# ═════════════════════════════════════════════
class PortfolioOptimizer:
    """진입 비중 계산.

    Usage:
        opt = PortfolioOptimizer(rest_api)
        info = opt.calculate_position_amount("005930", "1A")
        amount = info["amount"]
    """

    def __init__(
        self,
        rest_api,
        base_amount: int = DEFAULT_BASE_AMOUNT,
        kelly_cap: float = 0.25,           # 1/4 켈리
        kelly_baseline: float = 0.10,      # 켈리 0.10이 multiplier 1.0의 기준
        target_volatility: float = 0.02,   # 목표 일일 변동성 2%
        min_weight: float = 0.3,
        max_weight: float = 2.0,
        min_trades_for_kelly: int = 20,
        atr_period: int = 14,
        atr_candle_count: int = 30,
    ):
        self.api = rest_api
        self.base_amount = base_amount
        self.kelly_cap = kelly_cap
        self.kelly_baseline = kelly_baseline
        self.target_vol = target_volatility
        self.min_weight = min_weight
        self.max_weight = max_weight
        self.min_trades = min_trades_for_kelly
        self.atr_period = atr_period
        self.atr_candle_count = atr_candle_count

    def calculate_position_amount(
        self, stock_code: str, sub_strategy: str,
    ) -> dict:
        """최종 매수 금액 + 계산 근거 반환.

        Returns:
            {
              'amount': int,              # 1000원 단위로 반올림된 매수 금액
              'base': int,                # base_amount
              'kelly_fraction': float|None,
              'kelly_multiplier': float,
              'vol_multiplier': float,
              'atr_pct': float|None,
              'final_weight': float,
              'reasons': list[str],
            }
        """
        reasons = []

        # ── 1. Kelly multiplier
        kelly_f = self._compute_kelly(sub_strategy)
        if kelly_f is None:
            kelly_mult = 1.0
            reasons.append(f"Kelly: 데이터 부족 (<{self.min_trades}건) → 1.0x")
        elif kelly_f <= 0:
            kelly_mult = self.min_weight
            reasons.append(
                f"Kelly: f*={kelly_f:.3f} 손실 기대 → {kelly_mult}x (최소비중)"
            )
        else:
            kelly_mult = kelly_f / self.kelly_baseline
            reasons.append(
                f"Kelly: f*={kelly_f:.3f} → {kelly_mult:.2f}x "
                f"(baseline {self.kelly_baseline})"
            )

        # ── 2. Volatility multiplier
        vol_mult, atr_pct = self._compute_volatility(stock_code)
        if vol_mult is None:
            vol_mult = 1.0
            reasons.append("Vol: 분봉 부족 → 1.0x")
        else:
            reasons.append(
                f"Vol: ATR/price={atr_pct*100:.2f}% → {vol_mult:.2f}x "
                f"(target {self.target_vol*100:.1f}%)"
            )

        # ── 3. 통합 + 클리핑
        raw_weight = kelly_mult * vol_mult
        final_weight = max(self.min_weight, min(self.max_weight, raw_weight))
        if abs(final_weight - raw_weight) > 1e-6:
            reasons.append(
                f"클리핑: {raw_weight:.2f} → {final_weight:.2f} "
                f"({self.min_weight}~{self.max_weight})"
            )

        amount = int(self.base_amount * final_weight)
        amount = (amount // 1000) * 1000  # 1000원 단위 반올림

        return {
            "amount": amount,
            "base": self.base_amount,
            "kelly_fraction": kelly_f,
            "kelly_multiplier": kelly_mult,
            "vol_multiplier": vol_mult,
            "atr_pct": atr_pct,
            "final_weight": final_weight,
            "reasons": reasons,
        }

    # ─────────────────────────────────────────
    # Kelly 계산
    # ─────────────────────────────────────────
    def _compute_kelly(self, sub_strategy: str) -> Optional[float]:
        if TradeRepository is None:
            return None
        try:
            trades = TradeRepository.find_closed_by_substrategy(sub_strategy)
        except Exception as e:
            logger.warning("Kelly: DB 조회 실패 (%s): %s", sub_strategy, e)
            return None

        if not trades or len(trades) < self.min_trades:
            return None

        wins, losses = [], []
        for t in trades:
            p = _trade_profit(t)
            if p is None:
                continue
            if p > 0:
                wins.append(p)
            elif p < 0:
                losses.append(abs(p))

        if not wins or not losses:
            return None

        n_total = len(wins) + len(losses)
        win_rate = len(wins) / n_total
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)

        return kelly_fraction(
            win_rate, avg_win, avg_loss, max_fraction=self.kelly_cap
        )

    # ─────────────────────────────────────────
    # Volatility 계산
    # ─────────────────────────────────────────
    def _compute_volatility(
        self, stock_code: str,
    ) -> tuple[Optional[float], Optional[float]]:
        try:
            candles = self.api.get_minute_candles(
                stock_code, interval=1, count=self.atr_candle_count
            )
        except Exception as e:
            logger.warning("[%s] Vol: 분봉 조회 실패: %s", stock_code, e)
            return None, None

        if not candles or len(candles) < self.atr_period + 1:
            return None, None

        atr = compute_atr(candles, self.atr_period)
        current_price = candles[0].get("close")

        if not atr or not current_price or current_price <= 0:
            return None, None

        atr_pct = atr / current_price
        if atr_pct <= 0:
            return None, None

        mult = self.target_vol / atr_pct
        # 극단값 방어 (개별 multiplier 단계에서 0.2~3.0)
        mult = max(0.2, min(3.0, mult))
        return mult, atr_pct