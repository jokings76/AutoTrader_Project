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