"""
눌림목 N자 반등 진입 전략 (1A).
  9:30~10:40: 직전 고점 -1~3% 되돌림 + 5MA 터치 → 양봉·5MA 돌파 반등 → 점수.
평가/매수는 StrategyManager 기존 메서드 호출 어댑터.
"""
from __future__ import annotations
from datetime import time as dtime
from core.strategy.entries.base import EntryStrategy, EntryContext

SURGE_END = dtime(9, 30)
PHASE2_END = dtime(10, 40)


class PullbackStrategy(EntryStrategy):
    name = "pullback"
    sub_strategy = "1A"

    def is_active(self, now_time: dtime) -> bool:
        return SURGE_END <= now_time < PHASE2_END

    def evaluate(self, mgr, ctx: EntryContext):
        return mgr.evaluate_pullback(ctx.candles, ctx.stock_code)

    def can_buy(self, mgr) -> bool:
        return mgr.can_buy_phase1a()