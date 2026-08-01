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
진입 전략 레지스트리 — 시각에 맞는 활성 전략을 순서대로 제공.

on_condition_hit은 route(now_time)로 활성 전략 리스트를 받아 순서대로 평가한다.
순서가 우선순위 (먼저 통과한 전략으로 매수). 예: 9:30~10:40에서 surge → pullback.
"""
from __future__ import annotations

from datetime import time as dtime
from typing import List

from core.strategy.entries.base import EntryStrategy


class EntryRegistry:
    def __init__(self):
        self._strategies: List[EntryStrategy] = []

    def register(self, strategy: EntryStrategy) -> "EntryRegistry":
        self._strategies.append(strategy)
        return self

    def all(self) -> List[EntryStrategy]:
        return list(self._strategies)

    def route(self, now_time: dtime) -> List[EntryStrategy]:
        """now_time에 활성인 전략들을 등록 순서(=우선순위)대로 반환."""
        return [s for s in self._strategies if s.is_active(now_time)]