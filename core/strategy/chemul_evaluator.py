"""Phase 1B 체결강도 전략 FSM.

5단계 순차 조건 추적:
  1. 1분봉 눌림 (-1.5%) → WAITING_WALL
  2. 매도벽 등장 (WallState.DETECTED) → WAITING_SHRINK_STRENGTH
  3. 매도벽 축소 (SHRINKING) + 체결강도 급상승 → WAITING_DISAPPEAR
  4. 매도벽 소실 (DISAPPEARED) → READY_TO_BUY
  5. 매수 후 reset() 또는 타임아웃 → WAITING_PULLBACK 복귀

파라미터는 placeholder (실데이터 받고 튜닝 예정).
"""
import time
from enum import Enum
from typing import Optional

from core.strategy.orderbook import OrderbookTracker
from core.strategy.trade_flow import TradeFlowTracker
from core.strategy.wall_detector import WallDetector, WallState


class ChemulState(str, Enum):
    WAITING_PULLBACK = "waiting_pullback"
    WAITING_WALL = "waiting_wall"
    WAITING_SHRINK_STRENGTH = "waiting_shrink_strength"
    WAITING_DISAPPEAR = "waiting_disappear"
    READY_TO_BUY = "ready_to_buy"


class ChemulEvaluator:
    """체결강도 전략 — 종목별 5단계 FSM."""

    def __init__(
        self,
        trade_flow: TradeFlowTracker,
        wall_detector: WallDetector,
        orderbook: OrderbookTracker,
        # 파라미터 (TBD: 실데이터로 튜닝)
        pullback_pct: float = -1.5,          # 1분간 -1.5% 이하면 눌림
        pullback_window_sec: float = 60,
        strength_short_window: float = 10,
        strength_long_window: float = 30,
        strength_min: float = 180,            # 단기 가중 강도 임계값
        state_timeout_sec: float = 60,        # 각 상태 최대 체류 시간
    ):
        self.tf = trade_flow
        self.wd = wall_detector
        self.ob = orderbook

        self.pullback_pct = pullback_pct
        self.pullback_window_sec = pullback_window_sec
        self.strength_short_window = strength_short_window
        self.strength_long_window = strength_long_window
        self.strength_min = strength_min
        self.state_timeout_sec = state_timeout_sec

        # {code: {state, ts_entered_state}}
        self._state: dict[str, dict] = {}

    def evaluate(self, stock_code: str, now: float = None) -> ChemulState:
        """매 이벤트(체결/호가)마다 호출. 가능하면 여러 상태 연속 전이."""
        now = now if now is not None else time.time()

        s = self._state.get(stock_code)
        if s is None:
            s = {"state": ChemulState.WAITING_PULLBACK, "ts": now}
            self._state[stock_code] = s

        # 타임아웃 체크 (PULLBACK / READY_TO_BUY는 제외)
        if s["state"] not in (ChemulState.WAITING_PULLBACK, ChemulState.READY_TO_BUY):
            if now - s["ts"] > self.state_timeout_sec:
                s["state"] = ChemulState.WAITING_PULLBACK
                s["ts"] = now

        # 가능한 만큼 연속 전이
        while True:
            prev = s["state"]
            self._try_transition(stock_code, now)
            if s["state"] == prev:
                break

        return s["state"]

    def _try_transition(self, code: str, now: float):
        s = self._state[code]
        st = s["state"]

        if st == ChemulState.WAITING_PULLBACK:
            pct = self.tf.get_price_change_pct(
                code, self.pullback_window_sec, now
            )
            if pct is not None and pct <= self.pullback_pct:
                self._advance(code, ChemulState.WAITING_WALL, now)

        elif st == ChemulState.WAITING_WALL:
            ws = self.wd.get_state(code)
            # 이미 SHRINKING/DISAPPEARED라도 DETECTED를 거쳐온 거니 진행
            if ws in (WallState.DETECTED, WallState.SHRINKING, WallState.DISAPPEARED):
                self._advance(code, ChemulState.WAITING_SHRINK_STRENGTH, now)

        elif st == ChemulState.WAITING_SHRINK_STRENGTH:
            ws = self.wd.get_state(code)
            if ws in (WallState.SHRINKING, WallState.DISAPPEARED):
                if self.tf.is_strength_rising(
                    code,
                    short_window=self.strength_short_window,
                    long_window=self.strength_long_window,
                    now=now,
                    min_short_strength=self.strength_min,
                ):
                    self._advance(code, ChemulState.WAITING_DISAPPEAR, now)

        elif st == ChemulState.WAITING_DISAPPEAR:
            if self.wd.get_state(code) == WallState.DISAPPEARED:
                self._advance(code, ChemulState.READY_TO_BUY, now)

    def _advance(self, code: str, new_state: ChemulState, now: float):
        self._state[code]["state"] = new_state
        self._state[code]["ts"] = now

    def get_state(self, stock_code: str) -> ChemulState:
        s = self._state.get(stock_code)
        return s["state"] if s else ChemulState.WAITING_PULLBACK

    def get_info(self, stock_code: str) -> Optional[dict]:
        return self._state.get(stock_code)

    def reset(self, stock_code: str):
        """매수 성공 후 또는 강제 리셋."""
        self._state.pop(stock_code, None)