"""
VWAP(거래량가중평균가) 기반 필터 전략.

용도: 눌림목(pullback) 등 기존 점수 게이트를 통과한 종목에 대해,
      "지금 가격이 당일 평균 체결단가보다 위에 있는가"를 마지막 AND 필터로
      추가 적용해서 추세 이탈 종목을 걸러낸다.

기존 ScoreConfig(급등/양봉/거래량/강도 가중합산) 체계는 건드리지 않고,
그 위에 얹는 독립 게이트로 설계함 — 점수 계산 로직 수정 없이 안전하게 추가.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class VWAPConfig:
    # VWAP 위 몇 % 이상이면 강세로 볼 것인지
    strong_zone_pct: float = 0.005  # 0.5%
    # VWAP 이탈 허용 (이 값보다 더 아래로 떨어지면 약세)
    weak_zone_pct: float = -0.003  # -0.3%
    # 최대 점수
    max_score: int = 5


class VWAPStrategy:
    def __init__(self, config: VWAPConfig | None = None):
        self.config = config or VWAPConfig()

    def calculate_score(self, price: float, vwap: float) -> int:
        if vwap <= 0:
            return 0
        gap = (price - vwap) / vwap

        # 강한 상승
        if gap >= self.config.strong_zone_pct:
            return 5
        # 상승
        elif gap >= 0:
            return 4
        # VWAP 근처
        elif gap >= self.config.weak_zone_pct:
            return 2
        # 약세
        return 0

    def is_bullish(self, price: float, vwap: float) -> bool:
        return self.calculate_score(price, vwap) >= 4

    def evaluate(self, data: dict) -> dict:
        score = self.calculate_score(
            price=data["price"],
            vwap=data["vwap"],
        )
        return {
            "name": "VWAP",
            "score": score,
            "bullish": score >= 4,
        }


def calc_vwap(candles: list[dict]) -> float:
    """분봉 리스트로 VWAP(거래량가중평균가) 계산.

    VWAP = Σ(종가 × 거래량) / Σ(거래량)

    candles: [{"close": float, "volume": float, ...}, ...] — 순서 무관
             (당일 09:00부터 현재까지의 전체 분봉을 넘겨야 정확함.
              최근 N개만 넘기면 '최근 N분 평균가'가 되어 VWAP 본래
              의미(당일 누적 평균)와 달라지니 주의.)

    거래량 합이 0이거나 candles가 비어있으면 0.0 반환
    (호출부에서 vwap<=0 → calculate_score가 0점 처리하므로 안전).
    """
    if not candles:
        return 0.0

    total_value = 0.0
    total_volume = 0.0
    for c in candles:
        vol = c.get("volume", 0) or 0
        close = c.get("close", 0) or 0
        if vol <= 0 or close <= 0:
            continue
        total_value += close * vol
        total_volume += vol

    if total_volume <= 0:
        return 0.0
    return total_value / total_volume
