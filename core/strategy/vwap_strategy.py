"""
VWAP(거래대금 누적평균가) 기반 필터 전략.

용도: 눌림목(pullback) 등 기존 점수 게이팅을 통과한 종목에 대해
      "지금 가격이 당일 평균 체결가보다 위에 있는가"를 마지막 AND 필터로
      추가 적용해서 추세 이탈 종목을 거른다.

2026-07-25 확장: 고정 임계값 단일 판정에서 5가지 게이트 조합으로 확장.
  1) ATR 연동 적응형 임계값 — 변동성 큰 종목은 더 확실한 돌파만 인정
  2) VWAP 기울기 — VWAP 자체가 우상향 중인지 (횡보/우하향이면 탈락)
  3) VWAP reclaim 이벤트 — 최근 N개 캔들 중 VWAP 아래→위로 막 돌파했는지
  4) VWAP 밴드(표준편차) — VWAP+1σ 초과(과열)면 탈락
  6) 연속 confidence score — 이분법 대신 가중합 점수로 전환

각 기능은 VWAPConfig 플래그로 개별 on/off 가능 (장중 문제 생기면
해당 기능만 끄면 기존 동작으로 즉시 복귀).
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class VWAPConfig:
    # --- 기존 고정 임계값 (calculate_score, 하위호환용으로 유지) ---
    strong_zone_pct: float = 0.005  # VWAP 대비 +0.5% 이상이면 강한 상승
    weak_zone_pct: float = -0.003  # VWAP 대비 -0.3%까지는 근접 허용
    max_score: int = 5

    # --- 1) ATR 연동 적응형 임계값 ---
    use_atr_adaptive: bool = True
    atr_period: int = 14  # ATR 계산에 쓸 캔들 개수
    atr_multiplier: float = 0.3  # gap 임계값 = max(0, k * ATR%)

    # --- 2) VWAP 기울기 ---
    use_slope_check: bool = True
    slope_window: int = 5  # 최근 N개 시점의 VWAP 추세 확인

    # --- 3) VWAP reclaim(재돌파) 이벤트 ---
    use_reclaim: bool = True
    reclaim_lookback: int = 3  # 최근 N개 캔들 내 돌파 이벤트 탐색

    # --- 4) 과열 방지 밴드 (VWAP + 표준편차) ---
    use_band: bool = True
    band_lookback: int = 20  # 표준편차 계산 표본 개수
    band_std_mult: float = 1.0  # VWAP + 1σ 초과시 탈락

    # --- 6) 연속 confidence score ---
    # slope는 하드게이트(gate_slope)로 이미 걸러지므로 confidence에는 반영하지 않음
    # (이중 페널티 방지). 기존 weight_slope=0.3은 gap/volume에 재배분.
    use_continuous_score: bool = True
    weight_gap: float = 0.55
    weight_volume: float = 0.45
    min_confidence: float = 0.55  # confidence >= 이 값이어야 통과


class VWAPStrategy:
    def __init__(self, config: VWAPConfig | None = None):
        self.config = config or VWAPConfig()

    # ---- 기존 고정 임계값 방식 (하위호환) ----
    def calculate_score(self, price: float, vwap: float) -> int:
        if vwap <= 0:
            return 0
        gap = (price - vwap) / vwap

        if gap >= self.config.strong_zone_pct:
            return 5
        elif gap >= 0:
            return 4
        elif gap >= self.config.weak_zone_pct:
            return 2
        return 0

    def is_bullish(self, price: float, vwap: float) -> bool:
        return self.calculate_score(price, vwap) >= 4

    # ---- 신규: 종합 평가 ----
    def evaluate(self, data: dict) -> dict:
        """data: {"price": float, "vwap": float,
        "candles": list[dict] (선택, candles[0]=최신),
        "volume_ratio": float (선택, 기본 1.0)}
        """
        price = data["price"]
        vwap = data["vwap"]
        candles = data.get("candles") or []
        volume_ratio = data.get("volume_ratio", 1.0)
        cfg = self.config

        gap = (price - vwap) / vwap if vwap > 0 else -1.0
        base_score = self.calculate_score(price, vwap)

        # 1) ATR 적응형 임계값
        atr_pct = calc_atr_pct(candles, cfg.atr_period) if cfg.use_atr_adaptive else 0.0
        adaptive_threshold = (
            max(0.0, cfg.atr_multiplier * atr_pct) if cfg.use_atr_adaptive else 0.0
        )
        gate_adaptive = (gap >= adaptive_threshold) if cfg.use_atr_adaptive else True

        # 2) VWAP 기울기 (최근 slope_window 시점)
        gate_slope = True
        if cfg.use_slope_check and candles:
            series = calc_rolling_vwap(candles, cfg.slope_window)
            gate_slope = len(series) >= 2 and series[-1] > series[0]

        # 3) reclaim 이벤트 (최근 reclaim_lookback개 캔들 내 아래->위 돌파)
        gate_reclaim = True
        if cfg.use_reclaim and candles:
            n = cfg.reclaim_lookback + 1
            series = calc_rolling_vwap(candles, n)
            closes = [c.get("close", 0) or 0 for c in candles[:n]]
            closes = list(reversed(closes))  # 과거->최근으로 series와 정렬
            if len(series) >= 2:
                gate_reclaim = False
                for i in range(1, min(len(series), len(closes))):
                    if closes[i - 1] <= series[i - 1] and closes[i] > series[i]:
                        gate_reclaim = True
                        break

        # 4) 과열 방지 밴드
        gate_band = True
        if cfg.use_band and candles:
            std = calc_vwap_band_std(candles, vwap, cfg.band_lookback)
            if std > 0:
                gate_band = price <= vwap + cfg.band_std_mult * std

        gates = {
            "adaptive": gate_adaptive,
            "slope": gate_slope,
            "reclaim": gate_reclaim,
            "band": gate_band,
        }
        all_gates_pass = all(gates.values())

        confidence = None
        if cfg.use_continuous_score:
            gap_norm = min(max(gap / max(cfg.strong_zone_pct, 1e-6), 0.0), 1.0)
            vol_norm = min(max(volume_ratio - 1.0, 0.0), 2.0) / 2.0
            confidence = cfg.weight_gap * gap_norm + cfg.weight_volume * vol_norm
            score = round(confidence * cfg.max_score)
            bullish = (confidence >= cfg.min_confidence) and all_gates_pass
        else:
            score = base_score
            bullish = (base_score >= 4) and all_gates_pass

        return {
            "name": "VWAP",
            "score": score,
            "bullish": bullish,
            "gap_pct": gap * 100,
            "atr_pct": atr_pct * 100,
            "confidence": confidence,
            "gates": gates,
        }


def calc_vwap(candles: list[dict]) -> float:
    """분봉 리스트로 VWAP(거래대금 누적평균가) 계산.
    VWAP = Σ(종가 × 거래량) / Σ(거래량)
    candles: [{"close": float, "volume": float, ...}, ...] 순서 무관.
    거래량 합이 0이거나 candles가 비어있으면 0.0 반환.
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


def calc_rolling_vwap(candles: list[dict], points: int) -> list[float]:
    """candles[0]이 최신이라고 가정. 최근 `points`개 시점의 누적 VWAP을
    시간순(과거→최근)으로 반환. series[-1]이 가장 최근 시점(=calc_vwap(candles)와 동일).
    기울기/reclaim 판정용."""
    if not candles or points <= 0:
        return []
    n = min(points, len(candles))
    series = []
    for i in range(n - 1, -1, -1):
        series.append(calc_vwap(candles[i:]))
    return series


def calc_atr_pct(candles: list[dict], period: int = 14) -> float:
    """candles[0]이 최신이라고 가정. 최근 `period`개 캔들의 True Range 평균을
    최근 종가 대비 %로 반환. 표본 부족시 0.0 (적응형 임계값이 0%로 폴백)."""
    if not candles or len(candles) < 2:
        return 0.0
    n = min(period, len(candles) - 1)
    trs = []
    for i in range(n):
        cur, prev = candles[i], candles[i + 1]
        high = cur.get("high", cur.get("close", 0)) or 0
        low = cur.get("low", cur.get("close", 0)) or 0
        prev_close = prev.get("close", 0) or 0
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if not trs:
        return 0.0
    atr = sum(trs) / len(trs)
    last_close = candles[0].get("close", 0) or 0
    return (atr / last_close) if last_close > 0 else 0.0


def calc_vwap_band_std(candles: list[dict], vwap: float, points: int = 20) -> float:
    """최근 `points`개 종가가 VWAP에서 떨어진 정도의 표준편차(과열 밴드 폭 계산용).
    표본 부족시 0.0."""
    if not candles or vwap <= 0:
        return 0.0
    n = min(points, len(candles))
    diffs = [(c.get("close", 0) or 0) - vwap for c in candles[:n]]
    if len(diffs) < 2:
        return 0.0
    mean = sum(diffs) / len(diffs)
    var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
    return var**0.5
