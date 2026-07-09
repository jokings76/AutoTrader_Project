"""
급등 진입 전략 (1S).
  9:00~9:30 : 일반 임계값(score_cfg)
  9:30~10:40: strict 임계값(score_cfg_strict) — 추격 위험 구간
평가/매수는 StrategyManager 기존 메서드를 호출하는 어댑터 (동작 변경 없음).
"""
from __future__ import annotations
from datetime import time as dtime
from core.strategy.entries.base import EntryStrategy, EntryContext

SURGE_END = dtime(9, 30)
PHASE1_START = dtime(9, 0)
PHASE2_END = dtime(10, 40)


class SurgeStrategy(EntryStrategy):
    name = "surge"
    sub_strategy = "1S"

    def is_active(self, now_time: dtime) -> bool:
        return PHASE1_START <= now_time < PHASE2_END

    def evaluate(self, mgr, ctx: EntryContext):
        # 9:30 이후엔 strict cfg 사용
        if ctx.now_time is not None and ctx.now_time >= SURGE_END:
            cfg = mgr.score_cfg_strict
        else:
            cfg = None  # evaluate_surge가 None이면 기본 score_cfg 사용
        return mgr.evaluate_surge(ctx.candles, ctx.stock_code, cfg=cfg)

    def can_buy(self, mgr) -> bool:
        return mgr.can_buy_surge()

    def on_side_effect(self, mgr, ctx: EntryContext) -> None:
        # 9:00~9:30 급등 구간: 체결강도(1B) 감시 시작
        if ctx.now_time is not None and ctx.now_time < SURGE_END:
            if mgr.phase1b and not mgr.phase1b.is_watching(ctx.stock_code):
                mgr.phase1b.start_watching(ctx.stock_code)