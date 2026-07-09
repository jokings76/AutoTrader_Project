"""체결 틱 슬라이딩 윈도우 + 시간가중 체결강도 + 가격 추적.

KiwoomWS의 on_trade 콜백에서 add_tick() 호출.
체결강도 = 가중 매수체결량 / 가중 매도체결량 × 100
가격: get_latest_price / get_price_around / get_price_change_pct
"""
import math
import time
from collections import deque
from typing import Callable, Optional


class TradeFlowTracker:
    """체결 슬라이딩 윈도우 + 시간가중 강도 + 가격 추적."""

    def __init__(self, max_window_sec: float = 120):
        # {code: deque[(ts, price, side, volume)]}
        self.ticks: dict[str, deque] = {}
        self.max_window_sec = max_window_sec

    def add_tick(
        self,
        stock_code: str,
        price: int,
        side: str,
        volume: int,
        now: float = None,
    ):
        """0B 체결 1건 추가. side: 'buy'/'sell'/'neutral'."""
        now = now if now is not None else time.time()
        d = self.ticks.setdefault(stock_code, deque())
        d.append((now, price, side, volume))

        # 오래된 데이터 제거
        cutoff = now - self.max_window_sec
        while d and d[0][0] < cutoff:
            d.popleft()

    def compute_strength(
        self,
        stock_code: str,
        window_sec: float,
        now: float = None,
        weight_fn: Callable[[float, float], float] = None,
    ) -> float:
        """
        시간가중 체결강도 = 가중 매수체결량 / 가중 매도체결량 × 100.

        weight_fn(age_sec, window_sec) → 0~1. 기본은 선형 감쇠.
        매도가 0이면 9999 (매우 강한 매수 우세) 반환.
        """
        now = now if now is not None else time.time()
        d = self.ticks.get(stock_code)
        if not d:
            return 0.0

        weight_fn = weight_fn or self.linear_weight
        cutoff = now - window_sec
        w_buy = 0.0
        w_sell = 0.0
        for ts, price, side, vol in d:
            if ts < cutoff:
                continue
            w = weight_fn(now - ts, window_sec)
            if side == "buy":
                w_buy += vol * w
            elif side == "sell":
                w_sell += vol * w

        if w_sell == 0:
            return 9999.0 if w_buy > 0 else 0.0
        return w_buy / w_sell * 100

    def is_strength_rising(
        self,
        stock_code: str,
        short_window: float = 10,
        long_window: float = 30,
        now: float = None,
        min_short_strength: float = 0,
    ) -> bool:
        """단기 윈도우 강도가 장기 윈도우 강도보다 강한지."""
        s_short = self.compute_strength(stock_code, short_window, now)
        s_long = self.compute_strength(stock_code, long_window, now)
        return s_short > s_long and s_short >= min_short_strength

    def get_stats(
        self, stock_code: str, window_sec: float = 30, now: float = None
    ) -> dict:
        """디버그/관찰용 통계."""
        now = now if now is not None else time.time()
        d = self.ticks.get(stock_code, deque())
        cutoff = now - window_sec
        recent = [t for t in d if t[0] >= cutoff]
        buy_vol = sum(v for _, _, s, v in recent if s == "buy")
        sell_vol = sum(v for _, _, s, v in recent if s == "sell")
        simple = (buy_vol / sell_vol * 100) if sell_vol else (9999.0 if buy_vol else 0.0)
        return {
            "count": len(recent),
            "buy_vol": buy_vol,
            "sell_vol": sell_vol,
            "strength_simple": simple,
            "strength_weighted": self.compute_strength(stock_code, window_sec, now),
        }

    # ─── 가격 추적 ─────────────────────────────────
    def get_latest_price(self, stock_code: str) -> Optional[int]:
        """가장 최근 체결가."""
        d = self.ticks.get(stock_code)
        if not d:
            return None
        return d[-1][1]

    def get_price_around(
        self,
        stock_code: str,
        seconds_ago: float,
        now: float = None,
    ) -> Optional[int]:
        """N초 전 가격 = 그 시점 이전의 가장 최근 체결가.

        해당 시점보다 과거 데이터가 없으면 None.
        """
        now = now if now is not None else time.time()
        target = now - seconds_ago
        d = self.ticks.get(stock_code)
        if not d:
            return None
        # 최근 → 과거로 walking. target 이하의 첫 틱 = N초 전 가격
        for ts, price, side, vol in reversed(d):
            if ts <= target:
                return price
        return None

    def get_price_change_pct(
        self,
        stock_code: str,
        seconds_ago: float = 60,
        now: float = None,
    ) -> Optional[float]:
        """N초 전 대비 현재가 변화율 (%). 양수=상승, 음수=하락. 눌림 감지용."""
        cur = self.get_latest_price(stock_code)
        past = self.get_price_around(stock_code, seconds_ago, now)
        if cur is None or past is None or past == 0:
            return None
        return (cur - past) / past * 100
    # ──────────────────────────────────────────

    def reset(self, stock_code: str):
        self.ticks.pop(stock_code, None)

    # ─── 가중치 함수들 ──────────────────────────────
    @staticmethod
    def linear_weight(age: float, window: float) -> float:
        """선형 감쇠: age=0 → 1.0, age=window → 0.0."""
        if window <= 0:
            return 0.0
        return max(0.0, 1.0 - age / window)

    @staticmethod
    def exponential_weight(tau: float) -> Callable[[float, float], float]:
        """지수 감쇠 factory. tau가 작을수록 최근 가중치 ↑."""
        def fn(age: float, window: float) -> float:
            return math.exp(-age / tau)
        return fn