# ══════════════════════════════════════════════════════════════════
# ⚠️ [삭제 예정 / DEPRECATED] — 2026-08-02 표시
#
# 이 파일은 **어디서도 import되지 않는 폐기된 코드**다.
# (main.py 기준 도달성 분석으로 확인 — 라이브 모듈 34개에 포함되지 않음)
#
# 지금 살아있는 전략은 **1A / Pullback 두 개뿐**이고, 둘 다 틱 구동
# (체결강도 FID228 3초 연속 + 대량체결 버스트)으로 동작한다.
# 아래 코드는 그 이전 설계(Phase2/Phase3/Surge/WallDetector FSM 등)의
# 잔재이므로 **현재 동작의 근거로 삼으면 안 된다.**
#
# 남겨둔 이유: CLAUDE.md 작업규칙 2("파일 삭제 금지 — _legacy로 보존").
# 삭제해도 git 히스토리로 언제든 복구 가능하므로, 다윤님이 판단해서
# 정리하면 된다. 정리 시 이 배너가 붙은 파일 전체가 대상이다.
#   확인:  git ls-files "*_legacy.py"
#   삭제:  git rm $(git ls-files "*_legacy.py")
# ══════════════════════════════════════════════════════════════════

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