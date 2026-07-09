"""매도벽 상태머신 — 등장 → 축소 → 소실 추적.

OrderbookTracker를 참조해서 매도 1~2호가의 잔량 변화를 감시.
"""
from enum import Enum
from typing import Optional

from core.strategy.orderbook import OrderbookTracker


class WallState(str, Enum):
    IDLE = "idle"             # 벽 없음
    DETECTED = "detected"     # 벽 등장
    SHRINKING = "shrinking"   # 벽 축소 중
    DISAPPEARED = "disappeared"  # 벽 소실 → 매수 신호


class WallDetector:
    """
    매도벽 lifecycle:
      IDLE → (잔량 ≥ 이전 baseline × N배) → DETECTED
      DETECTED → (잔량 ≤ 초기 × shrink_ratio) → SHRINKING
      SHRINKING → (잔량 ≤ 초기 × disappear_ratio) → DISAPPEARED

    파라미터는 일단 placeholder (실데이터 받고 튜닝 예정).
    """

    def __init__(
        self,
        ob_tracker: OrderbookTracker,
        detect_multiplier: float = 5.0,     # TBD: 실데이터 분포 보고
        shrink_ratio: float = 0.7,           # TBD
        disappear_ratio: float = 0.2,        # TBD
        avg_window_sec: float = 60,
        watch_levels: tuple[int, ...] = (1, 2),
    ):
        self.ob = ob_tracker
        self.detect_multiplier = detect_multiplier
        self.shrink_ratio = shrink_ratio
        self.disappear_ratio = disappear_ratio
        self.avg_window_sec = avg_window_sec
        self.watch_levels = watch_levels

        # {code: {state, initial_volume, level, ts_detected}}
        self._state: dict[str, dict] = {}

    def on_orderbook(self, stock_code: str, now: float = None) -> WallState:
        """0D 수신 후 OrderbookTracker.update가 끝난 다음에 호출."""
        info = self._state.get(stock_code)
        st = info["state"] if info else WallState.IDLE

        if st == WallState.IDLE:
            return self._try_detect(stock_code, now)

        if st == WallState.DETECTED:
            return self._try_shrink(stock_code, now)

        if st == WallState.SHRINKING:
            return self._try_disappear(stock_code, now)

        # DISAPPEARED 이후는 외부에서 reset() 호출 전까지 유지
        return st

    def _try_detect(self, code: str, now: float) -> WallState:
        for level in self.watch_levels:
            vol_now = self.ob.get_ask_volume(code, level)
            # ★ exclude_latest=True: baseline은 현재 스파이크를 제외한 이전 평균
            vol_avg = self.ob.get_ask_volume_avg(
                code, level, self.avg_window_sec, exclude_latest=True
            )
            if vol_avg <= 0 or vol_now <= 0:
                continue
            if vol_now >= vol_avg * self.detect_multiplier:
                self._state[code] = {
                    "state": WallState.DETECTED,
                    "initial_volume": vol_now,
                    "level": level,
                    "ts_detected": now,
                }
                return WallState.DETECTED
        return WallState.IDLE

    def _try_shrink(self, code: str, now: float) -> WallState:
        info = self._state[code]
        level = info["level"]
        vol_now = self.ob.get_ask_volume(code, level)
        # 벽이 더 커지면 초기값 갱신
        if vol_now > info["initial_volume"]:
            info["initial_volume"] = vol_now
            return WallState.DETECTED
        if info["initial_volume"] > 0 and vol_now <= info["initial_volume"] * self.shrink_ratio:
            info["state"] = WallState.SHRINKING
            return WallState.SHRINKING
        return WallState.DETECTED

    def _try_disappear(self, code: str, now: float) -> WallState:
        info = self._state[code]
        level = info["level"]
        vol_now = self.ob.get_ask_volume(code, level)
        if info["initial_volume"] > 0 and vol_now <= info["initial_volume"] * self.disappear_ratio:
            info["state"] = WallState.DISAPPEARED
            return WallState.DISAPPEARED
        return WallState.SHRINKING

    def get_state(self, stock_code: str) -> WallState:
        info = self._state.get(stock_code)
        return info["state"] if info else WallState.IDLE

    def get_info(self, stock_code: str) -> Optional[dict]:
        return self._state.get(stock_code)

    def reset(self, stock_code: str):
        self._state.pop(stock_code, None)