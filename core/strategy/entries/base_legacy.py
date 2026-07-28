"""
진입 전략 공통 인터페이스 (EntryStrategy) + 컨텍스트(EntryContext).

목적: 시간대별 진입 로직(급등/눌림목/오후장)을 독립 모듈로 분리.
  - StrategyManager.on_condition_hit 안의 거대한 if/elif 분기를 제거
  - 전략 추가 = entries/에 파일 1개 + registry 등록 1줄 (manager 무수정)
  - 전략끼리 변수 공유 없음 → 충돌 방지

계약(contract):
  각 전략은 EntryStrategy를 상속하고 아래를 구현/설정한다.
    name          : 로그용 식별자 ("surge"/"pullback"/"phase3")
    sub_strategy  : holdings 분류 태그 ("1S"/"1A"/"3")
    is_active(t)  : 현재 시각 t(datetime.time)에 이 전략이 동작하는가
    evaluate(mgr, ctx) -> (ok: bool, info: dict)
                    : 매수해야 하는가 + 평가 정보(점수 등)
    can_buy(mgr)  -> bool : 슬롯/시간 게이트 (기존 can_buy_* 재사용)
    on_side_effect(mgr, ctx) -> None (선택)
                    : 매수와 무관한 부수효과(예: 1B 감시 시작, Phase3 감시 시작)
                      매 호출마다 실행됨. 기본 no-op.

설계 원칙:
  - 전략은 mgr(StrategyManager)의 기존 메서드를 호출하는 얇은 어댑터로 시작.
    (로직 복제 없음 → 분리로 인한 동작 변경 0)
  - 점진적으로 로직을 각 파일로 내재화 가능.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Optional


@dataclass
class EntryContext:
    """전략 평가에 필요한 입력 묶음. on_condition_hit이 1회 구성해 전략들에 전달."""
    stock_code: str
    stock_name: str
    candles: list = field(default_factory=list)
    now_time: Optional[dtime] = None
    phase: Optional[int] = None
    # 필요 시 확장 (volume_ratio, strength 등은 mgr 헬퍼로 전략이 직접 조회)


class EntryStrategy:
    """진입 전략 베이스. 하위 클래스가 name/sub_strategy를 설정하고 메서드를 구현."""
    name: str = "base"
    sub_strategy: str = "?"

    def is_active(self, now_time: dtime) -> bool:
        """이 전략이 now_time에 활성인가."""
        raise NotImplementedError

    def evaluate(self, mgr, ctx: EntryContext):
        """(ok, info) 반환. ok=True면 매수 후보."""
        raise NotImplementedError

    def can_buy(self, mgr) -> bool:
        """슬롯/시간 게이트. 기본 True(전략별 오버라이드)."""
        return True

    def on_side_effect(self, mgr, ctx: EntryContext) -> None:
        """매수와 무관한 부수효과(감시 시작 등). 기본 no-op."""
        return None