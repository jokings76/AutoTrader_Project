"""실시간 호가창 스냅샷 트래커.

KiwoomWS의 on_orderbook 콜백에서 update() 호출.
WallDetector가 이걸 참조해서 매도벽 추적.
"""
import time
from collections import deque
from typing import Optional


class OrderbookTracker:
    """종목별 호가창 최신 상태 + 잔량 히스토리."""

    def __init__(self, history_window_sec: float = 60):
        # 현재 스냅샷: {code: snapshot_dict}
        self.snapshots: dict[str, dict] = {}
        # 호가 잔량 히스토리: {code: deque[(ts, ask_volumes_list, bid_volumes_list)]}
        self.history: dict[str, deque] = {}
        self.history_window_sec = history_window_sec

    def update(self, stock_code: str, snapshot: dict, now: float = None):
        """0D 메시지 들어올 때마다 호출."""
        now = now if now is not None else time.time()
        self.snapshots[stock_code] = snapshot

        h = self.history.setdefault(stock_code, deque())
        h.append((
            now,
            list(snapshot.get("ask_volumes") or []),
            list(snapshot.get("bid_volumes") or []),
        ))
        # 오래된 데이터 제거
        cutoff = now - self.history_window_sec
        while h and h[0][0] < cutoff:
            h.popleft()

    def get_snapshot(self, stock_code: str) -> Optional[dict]:
        return self.snapshots.get(stock_code)

    def get_ask_volume(self, stock_code: str, level: int = 1) -> int:
        """현재 매도 N호가 잔량 (level=1~10)."""
        snap = self.snapshots.get(stock_code)
        if not snap:
            return 0
        vols = snap.get("ask_volumes") or []
        idx = level - 1
        return vols[idx] if 0 <= idx < len(vols) else 0

    def get_bid_volume(self, stock_code: str, level: int = 1) -> int:
        snap = self.snapshots.get(stock_code)
        if not snap:
            return 0
        vols = snap.get("bid_volumes") or []
        idx = level - 1
        return vols[idx] if 0 <= idx < len(vols) else 0

    def get_ask_volume_avg(
        self,
        stock_code: str,
        level: int = 1,
        window_sec: float = None,
        exclude_latest: bool = False,
    ) -> float:
        """매도 N호가의 직전 window_sec 동안 평균 잔량.

        window_sec=None이면 전체 history 사용.
        exclude_latest=True면 가장 최근 스냅샷 제외 (벽 감지 baseline 산출용).
        """
        h = self.history.get(stock_code)
        if not h:
            return 0.0
        if window_sec is not None:
            now = h[-1][0]
            cutoff = now - window_sec
            samples = [ask for ts, ask, _ in h if ts >= cutoff]
        else:
            samples = [ask for _, ask, _ in h]

        if exclude_latest:
            if len(samples) <= 1:
                return 0.0  # baseline 잡을 데이터 없음
            samples = samples[:-1]

        idx = level - 1
        values = [s[idx] for s in samples if idx < len(s)]
        return sum(values) / len(values) if values else 0.0

    def get_top_ask(self, stock_code: str) -> tuple[Optional[int], int]:
        """현재 매도 1호가 (price, volume). 없으면 (None, 0)."""
        snap = self.snapshots.get(stock_code)
        if not snap:
            return None, 0
        prices = snap.get("ask_prices") or []
        vols = snap.get("ask_volumes") or []
        if not prices:
            return None, 0
        return prices[0], (vols[0] if vols else 0)

    def get_top_bid(self, stock_code: str) -> tuple[Optional[int], int]:
        snap = self.snapshots.get(stock_code)
        if not snap:
            return None, 0
        prices = snap.get("bid_prices") or []
        vols = snap.get("bid_volumes") or []
        if not prices:
            return None, 0
        return prices[0], (vols[0] if vols else 0)

    def reset(self, stock_code: str):
        self.snapshots.pop(stock_code, None)
        self.history.pop(stock_code, None)