"""
오후장 진입 전략 (3).
  10:41~15:00: 체결강도 FSM(150 터치) 트리거 → 점수 게이트(evaluate_phase3_rank).
주의: Phase3는 '편입 즉시 평가'가 아니라 'FSM 감시 시작' 후 체결로 트리거됨.
      따라서 evaluate는 항상 (False, ...)를 반환하고, 실제 진입은
      on_side_effect에서 감시 시작 → on_trade(FSM) → _try_phase3_buy 경로로 처리.
"""
from __future__ import annotations
from datetime import time as dtime
from core.strategy.entries.base import EntryStrategy, EntryContext

PHASE3_START = dtime(10, 41)
PHASE3_END = dtime(15, 0)


class Phase3Strategy(EntryStrategy):
    name = "phase3"
    sub_strategy = "3"

    def is_active(self, now_time: dtime) -> bool:
        return PHASE3_START <= now_time < PHASE3_END

    def evaluate(self, mgr, ctx: EntryContext):
        # Phase3는 즉시 매수가 아니라 FSM 감시 경로 → 여기선 매수 후보 아님
        return False, {"reason": "phase3: FSM 감시 경로"}

    def can_buy(self, mgr) -> bool:
        return mgr.can_buy_phase3()

    def on_side_effect(self, mgr, ctx: EntryContext) -> None:
        # Phase3 감시 시작 (슬롯 있으면)
        if not mgr.phase3:
            return
        if not mgr.can_buy_phase3():
            return
        if not mgr.phase3.is_watching(ctx.stock_code):
            mgr.phase3.start_watching(ctx.stock_code)