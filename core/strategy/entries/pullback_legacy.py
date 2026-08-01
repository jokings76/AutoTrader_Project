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