# core/strategy/scoring.py
"""
점수 기반 진입 평가 (AND 필터 → 가중 점수) + 체결강도 필터 완벽 결합
===================================================================
가짜 체결강도(140% 이상 과열 + 거래량 부족 or 음봉)를 원천 차단하는 로직이 추가되었습니다.
"""

from __future__ import annotations
from dataclasses import dataclass
from core.strategy.indicators import calc_ma

# ===================== 설정 ===================== #
@dataclass
class ScoreConfig:
    # 가중치 (Weights)
    w_surge: float = 3.0
    w_ma: float = 2.0
    w_candle: float = 2.0
    w_volume: float = 3.0
    w_strength: float = 2.0         # 체결강도 가중치 (추가됨)

    # 만점 기준점
    surge_target: float = 0.05      # Phase1 SURGE_THRESHOLD (만점 기준 상승률)
    surge_min: float = 0.03         # Surge SURGE_ENTRY_MIN (0이면 급등% 요소 끔)
    ma_tolerance: float = 0.003     # MA_TOUCH_TOLERANCE (이 안이면 MA 만점)
    ma_band: float = 0.010          # tol 초과 후 0점까지의 폭(1%)
    volume_target: float = 1.5      # VOLUME_SURGE_RATIO (만점 기준 거래량배수)
    doji_credit: float = 0.4        # 도지(close==open) 부분점수
    strength_target: float = 120.0  # 120% 이상일 때 체결강도 만점 (추가됨)

    # 통과 기준
    threshold_ratio: float = 0.7
    threshold_abs: float | None = None

DEFAULT = ScoreConfig()

# ===================== 요소별 factor (0~1) ===================== #
def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))

def _f_surge(surge_rate: float, target: float) -> float:
    if target <= 0:
        return 1.0
    return _clamp(surge_rate / target)

def _f_ma(current: float, ma: float | None, tol: float, band: float) -> float:
    if not ma:
        return 0.0
    d = abs(current - ma) / ma
    if d <= tol:
        return 1.0
    return _clamp(1.0 - (d - tol) / band)

def _f_candle(cur: dict, doji_credit: float) -> float:
    o, c = cur["open"], cur["close"]
    if c > o:
        return 1.0
    if c == o:
        return doji_credit
    return 0.0

def _f_volume(vol_ratio: float, target: float) -> float:
    if target <= 0:
        return 1.0
    return _clamp(vol_ratio / target)

# [핵심] 가짜 체결강도 필터링 로직
def _f_strength(strength: float, cur_candle: dict, vol_ratio: float, target: float = 120.0) -> float:
    if strength < 100:
        return 0.0
    
    # 음봉 판별
    is_bearish = cur_candle["close"] < cur_candle["open"]
    
    # 140% 이상 과열 상태인데, 음봉이거나 거래량이 평소 1배도 안 되면(봇의 장난) 가차없이 0점!
    if strength >= 140:
        if is_bearish or vol_ratio < 1.0:
            return 0.0
            
    return _clamp((strength - 100) / (target - 100))

# ===================== 공통 마감 ===================== #
def _finalize(parts: dict, maxw: dict, cfg: ScoreConfig) -> tuple[bool, float, float, str]:
    total = sum(parts.values())
    max_total = sum(maxw.values())
    thr = cfg.threshold_abs if cfg.threshold_abs is not None else cfg.threshold_ratio * max_total
    passed = total >= thr
    # 체결강도(strength) 라벨 추가
    label_map = {"surge": "급등", "ma": "MA", "candle": "양봉", "volume": "거래량", "strength": "강도"}
    seg = ", ".join(f"{label_map.get(k, k)} {parts[k]:.1f}/{maxw[k]:.0f}" for k in parts)
    reason = f"점수 {total:.1f}/{max_total:.0f} < {thr:.1f} ({seg})"
    return passed, total, thr, reason

def _breakdown(parts: dict) -> dict:
    return {k: round(v, 2) for k, v in parts.items()}

# ===================== Phase 1A ===================== #
def score_phase1(candles: list[dict], volume_ratio: float, current_strength: float = 100.0, cfg: ScoreConfig = DEFAULT):
    cur = candles[0]
    open_price = candles[-1]["open"]
    current_price = cur["close"]
    surge_rate = (current_price - open_price) / open_price if open_price else 0.0
    ma5 = calc_ma(candles, 5)

    parts = {
        "surge":  cfg.w_surge   * _f_surge(surge_rate, cfg.surge_target),
        "ma":     cfg.w_ma      * _f_ma(current_price, ma5, cfg.ma_tolerance, cfg.ma_band),
        "candle": cfg.w_candle  * _f_candle(cur, cfg.doji_credit),
        "volume": cfg.w_volume  * _f_volume(volume_ratio, cfg.volume_target),
        "strength": cfg.w_strength * _f_strength(current_strength, cur, volume_ratio, cfg.strength_target),
    }
    maxw = {"surge": cfg.w_surge, "ma": cfg.w_ma, "candle": cfg.w_candle, "volume": cfg.w_volume, "strength": cfg.w_strength}
    passed, total, thr, reason = _finalize(parts, maxw, cfg)

    info = {
        "current_price": current_price, "open_price": open_price,
        "surge_rate": surge_rate, "ma5": ma5, "volume_ratio": volume_ratio,
        "current_strength": current_strength,
        "score": round(total, 2), "score_threshold": round(thr, 2),
        "score_breakdown": _breakdown(parts),
    }
    if not passed:
        info["reason"] = reason
    return passed, info

# ===================== Surge (1S) — MA 없음 ===================== #
def score_surge(candles: list[dict], volume_ratio: float, current_strength: float = 100.0, cfg: ScoreConfig = DEFAULT):
    cur = candles[0]
    open_price = candles[-1]["open"]
    current_price = cur["close"]
    surge_rate = (current_price - open_price) / open_price if open_price else 0.0

    w_surge = cfg.w_surge if cfg.surge_min > 0 else 0.0
    parts = {
        "surge":  w_surge      * _f_surge(surge_rate, cfg.surge_min),
        "candle": cfg.w_candle * _f_candle(cur, cfg.doji_credit),
        "volume": cfg.w_volume * _f_volume(volume_ratio, cfg.volume_target),
        "strength": cfg.w_strength * _f_strength(current_strength, cur, volume_ratio, cfg.strength_target),
    }
    maxw = {"surge": w_surge, "candle": cfg.w_candle, "volume": cfg.w_volume, "strength": cfg.w_strength}
    passed, total, thr, reason = _finalize(parts, maxw, cfg)

    info = {
        "current_price": current_price, "surge_rate": surge_rate,
        "volume_ratio": volume_ratio, "current_strength": current_strength,
        "score": round(total, 2), "score_threshold": round(thr, 2),
        "score_breakdown": _breakdown(parts),
    }
    if not passed:
        info["reason"] = reason
    return passed, info

# ===================== Phase 2 (NN MA) ===================== #
def score_phase2(candles: list[dict], volume_ratio: float, ma_period: int, current_strength: float = 100.0, cfg: ScoreConfig = DEFAULT):
    cur = candles[0]
    current_price = cur["close"]
    ma = calc_ma(candles, ma_period)

    parts = {
        "ma":     cfg.w_ma      * _f_ma(current_price, ma, cfg.ma_tolerance, cfg.ma_band),
        "candle": cfg.w_candle  * _f_candle(cur, cfg.doji_credit),
        "volume": cfg.w_volume  * _f_volume(volume_ratio, cfg.volume_target),
        "strength": cfg.w_strength * _f_strength(current_strength, cur, volume_ratio, cfg.strength_target),
    }
    maxw = {"ma": cfg.w_ma, "candle": cfg.w_candle, "volume": cfg.w_volume, "strength": cfg.w_strength}
    passed, total, thr, reason = _finalize(parts, maxw, cfg)

    info = {
        "current_price": current_price, "ma5": ma, "volume_ratio": volume_ratio,
        "current_strength": current_strength,
        "score": round(total, 2), "score_threshold": round(thr, 2),
        "score_breakdown": _breakdown(parts),
    }
    if not passed:
        info["reason"] = reason
    return passed, info
# =====================================================================
# ↓↓↓ 아래 전체를 scoring.py "맨 아래"에 그대로 추가하세요 (기존 코드 수정 없음) ↓↓↓
# =====================================================================

# ===================== 눌림목 N자 반등 (9:31~10:40) ===================== #
# 패턴: 직전 고점 대비 1~3% 되돌림 + 5MA 터치(눌림) → 양봉 + 종가 5MA 돌파(반등)
#        → 통과 시 양봉/거래량/체결강도 점수로 최종 판정.
# strength 컴포넌트 포함(기존 score_* 와 동일 계약: (passed, info)).
PULLBACK_LOOKBACK   = 10     # 되돌림 탐색 윈도우(분봉 개수)
PULLBACK_MIN_DROP   = 0.01   # 직전 고점 대비 최소 되돌림(1%)
PULLBACK_MAX_DROP   = 0.03   # 직전 고점 대비 최대 되돌림(3%)


def _pullback_setup(candles: list[dict], ma5: float | None, ma_tol: float):
    """되돌림+5MA터치(눌림)가 성립했는지 판정. (ok, drop_rate, recent_high) 반환.
    candles[0]이 최신(현재 봉)."""
    if not ma5:
        return False, 0.0, 0.0
    window = candles[:PULLBACK_LOOKBACK]
    if len(window) < 3:
        return False, 0.0, 0.0
    recent_high = max(c["high"] for c in window)
    if recent_high <= 0:
        return False, 0.0, 0.0
    # 고점 이후의 최저가(되돌림 저점) — 윈도우 내 최저 저가로 근사
    pullback_low = min(c["low"] for c in window)
    drop_rate = (recent_high - pullback_low) / recent_high
    # 1~3% 되돌림
    if not (PULLBACK_MIN_DROP <= drop_rate <= PULLBACK_MAX_DROP):
        return False, drop_rate, recent_high
    # 되돌림 구간에서 5MA 터치(저가가 5MA의 tol 이내로 근접)
    touched = any(abs(c["low"] - ma5) / ma5 <= ma_tol for c in window if ma5)
    if not touched:
        return False, drop_rate, recent_high
    return True, drop_rate, recent_high


def score_pullback(candles: list[dict], volume_ratio: float,
                   current_strength: float = 100.0, cfg: ScoreConfig = DEFAULT):
    """눌림목 반등 평가. 눌림(되돌림+5MA터치) 미성립이면 즉시 False.
    성립 시 반등(양봉+5MA돌파) 확인 후 양봉/거래량/체결강도 점수화."""
    cur = candles[0]
    current_price = cur["close"]
    ma5 = calc_ma(candles, 5)

    setup_ok, drop_rate, recent_high = _pullback_setup(candles, ma5, cfg.ma_tolerance)
    if not setup_ok:
        info = {
            "current_price": current_price, "ma5": ma5,
            "volume_ratio": volume_ratio, "current_strength": current_strength,
            "drop_rate": drop_rate,
            "reason": f"눌림 미성립 (되돌림 {drop_rate*100:.2f}%, 1~3% 밖 또는 5MA 미터치)",
        }
        return False, info

    # 반등 트리거: 양봉 + 종가가 5MA 위
    rebound = (cur["close"] > cur["open"]) and (ma5 and current_price > ma5)
    if not rebound:
        info = {
            "current_price": current_price, "ma5": ma5,
            "volume_ratio": volume_ratio, "current_strength": current_strength,
            "drop_rate": drop_rate,
            "reason": f"눌림 OK but 반등 미확인 (양봉·5MA돌파 필요, 되돌림 {drop_rate*100:.2f}%)",
        }
        return False, info

    # 반등 확인 → 양봉/거래량/체결강도 점수화 (MA는 이미 돌파 확인했으니 만점 부여)
    parts = {
        "ma":       cfg.w_ma     * 1.0,
        "candle":   cfg.w_candle * _f_candle(cur, cfg.doji_credit),
        "volume":   cfg.w_volume * _f_volume(volume_ratio, cfg.volume_target),
        "strength": cfg.w_strength * _f_strength(current_strength, cur, volume_ratio, cfg.strength_target),
    }
    maxw = {"ma": cfg.w_ma, "candle": cfg.w_candle,
            "volume": cfg.w_volume, "strength": cfg.w_strength}
    passed, total, thr, reason = _finalize(parts, maxw, cfg)

    info = {
        "current_price": current_price, "ma5": ma5,
        "volume_ratio": volume_ratio, "current_strength": current_strength,
        "drop_rate": drop_rate, "recent_high": recent_high,
        "score": round(total, 2), "score_threshold": round(thr, 2),
        "score_breakdown": _breakdown(parts),
    }
    if not passed:
        info["reason"] = reason
    return passed, info
# =====================================================================
# ↓↓↓ 아래 전체를 scoring.py "맨 아래"에 그대로 추가하세요 (기존 코드 수정 없음) ↓↓↓
#     ※ score_pullback 아래에 이어서 붙이면 됩니다.
# =====================================================================

# ===================== 오후장 점수 랭크 (10:41~15:00) ===================== #
# 이중 게이트: 체결강도 FSM이 타이밍을 잡고, 이 점수가 '살 만한가'를 거름.
# 요소: 체결강도(매수세) + 거래량 + 양봉 + 당일위치(과열 방지).
#   - 당일위치: 현재가가 당일 고가(candles 윈도우 최고가 근사)에 -3% 이내로
#     너무 붙어있으면 과열로 감점, 적당히 눌려있으면 가점.
# strength 컴포넌트 포함. 기존 score_* 와 동일 계약: (passed, info).
PHASE3_W_STRENGTH   = 3.0    # 체결강도 (오후장 핵심)
PHASE3_W_VOLUME     = 2.5    # 거래량
PHASE3_W_CANDLE     = 1.5    # 양봉
PHASE3_W_POSITION   = 2.0    # 당일 위치(과열 방지)
PHASE3_OVERHEAT_PCT = 0.03   # 당일고가 -3% 이내면 과열(위치점수 0)
PHASE3_PULLBACK_PCT = 0.07   # 당일고가 -7% 이상 눌리면 위치점수 만점(그 사이 선형)


def _f_position(current_price: float, day_high: float,
                overheat: float, pullback: float) -> float:
    """당일 고가 대비 현재가 위치 점수(0~1).
    고가 -overheat 이내(너무 꼭대기)=0, 고가 -pullback 이상 눌림=1.0, 사이 선형."""
    if not day_high or day_high <= 0:
        return 0.5  # 고가 불명 시 중립
    dist = (day_high - current_price) / day_high   # 고가 아래로 얼마나(양수)
    if dist <= overheat:
        return 0.0                                  # 과열(추격 금지)
    if dist >= pullback:
        return 1.0                                  # 충분히 눌림
    return (dist - overheat) / (pullback - overheat)


def score_phase3_rank(candles: list[dict], volume_ratio: float,
                      current_strength: float = 100.0, cfg: ScoreConfig = DEFAULT):
    """오후장(10:41~) 점수 게이트. 당일 고가는 candles 윈도우 최고가로 근사.
    가중치는 오후장 전용 상수(PHASE3_W_*)를 사용 — 오전 cfg 가중치와 분리."""
    cur = candles[0]
    current_price = cur["close"]
    day_high = max(c["high"] for c in candles) if candles else 0.0

    parts = {
        "strength": PHASE3_W_STRENGTH * _f_strength(current_strength, cur, volume_ratio, cfg.strength_target),
        "volume":   PHASE3_W_VOLUME   * _f_volume(volume_ratio, cfg.volume_target),
        "candle":   PHASE3_W_CANDLE   * _f_candle(cur, cfg.doji_credit),
        "position": PHASE3_W_POSITION * _f_position(current_price, day_high,
                                                    PHASE3_OVERHEAT_PCT, PHASE3_PULLBACK_PCT),
    }
    maxw = {"strength": PHASE3_W_STRENGTH, "volume": PHASE3_W_VOLUME,
            "candle": PHASE3_W_CANDLE, "position": PHASE3_W_POSITION}

    total = sum(parts.values())
    max_total = sum(maxw.values())
    thr = cfg.threshold_abs if cfg.threshold_abs is not None else cfg.threshold_ratio * max_total
    passed = total >= thr

    pos_dist = (day_high - current_price) / day_high if day_high else 0.0
    label = {"strength": "체결강도", "volume": "거래량", "candle": "양봉", "position": "위치"}
    seg = ", ".join(f"{label[k]} {parts[k]:.1f}/{maxw[k]:.0f}" for k in parts)
    reason = f"오후 점수 {total:.1f}/{max_total:.0f} < {thr:.1f} ({seg})"

    info = {
        "current_price": current_price, "volume_ratio": volume_ratio,
        "current_strength": current_strength,
        "day_high": day_high, "position_dist": round(pos_dist, 4),
        "score": round(total, 2), "score_threshold": round(thr, 2),
        "score_breakdown": _breakdown(parts),
    }
    if not passed:
        info["reason"] = reason
    return passed, info