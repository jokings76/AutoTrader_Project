"""
조건검색 포착 종목의 과거 거래대금 폭발 이력 검증 + 실시간 스코어링 + 종가베팅 후보 선정.
core.history_fetcher.aggregate_ticks_to_bins()로 재집계된 bin 리스트를 입력으로 사용.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import numpy as np


@dataclass
class ExplosionConfig:
    min_bars_for_3m_baseline: int = 500
    slot_minutes: int = 10
    percentile: float = 0.9

    threshold_60m_krw: float = 6_000_000_000
    time_ratio: float = 3 / 60

    tier_high_ratio: float = 1.0
    tier_watch_ratio: float = 0.7
    score_high: float = 50.0
    score_watch: float = 25.0
    score_high_bonus_cap: float = 30.0

    closing_window_start_hm: tuple = (14, 50)
    closing_window_end_hm: tuple = (15, 20)


def build_3m_baseline(bins_3m: list[dict], config: ExplosionConfig) -> dict | None:
    if not bins_3m or len(bins_3m) < config.min_bars_for_3m_baseline:
        return None

    bullish_bins = [b for b in bins_3m if b["bullish"]]
    if not bullish_bins:
        return None

    slot_values: dict[str, list[float]] = {}
    for b in bullish_bins:
        slot_min = (b["dt"].minute // config.slot_minutes) * config.slot_minutes
        slot = f"{b['dt'].hour:02d}:{slot_min:02d}"
        slot_values.setdefault(slot, []).append(b["trade_value"])

    slot_thresholds = {s: float(np.percentile(v, config.percentile * 100)) for s, v in slot_values.items()}
    slot_counts = {s: len(v) for s, v in slot_values.items()}
    overall_values = [b["trade_value"] for b in bullish_bins]
    overall_threshold = float(np.percentile(overall_values, config.percentile * 100))

    return {
        "slot_thresholds": slot_thresholds,
        "slot_counts": slot_counts,
        "overall_threshold": overall_threshold,
        "sample_bars": len(bins_3m),
    }


def build_60m_fallback_threshold(bins_60m: list[dict], config: ExplosionConfig, hour: int) -> float:
    base = config.threshold_60m_krw * config.time_ratio
    if not bins_60m:
        return base
    values = [b["trade_value"] for b in bins_60m]
    overall_mean = sum(values) / len(values) if values else 0.0
    if overall_mean <= 0:
        return base
    hour_values = [b["trade_value"] for b in bins_60m if b["dt"].hour == hour]
    if not hour_values:
        return base
    hour_mean = sum(hour_values) / len(hour_values)
    weight = hour_mean / overall_mean if hour_mean > 0 else 1.0
    return base * weight


def get_threshold_for_now(baseline: dict | None, bins_60m: list[dict], config: ExplosionConfig, now: datetime) -> tuple[float, str]:
    if baseline is not None:
        slot_min = (now.minute // config.slot_minutes) * config.slot_minutes
        slot_label = f"{now.hour:02d}:{slot_min:02d}"
        if slot_label in baseline["slot_thresholds"] and baseline["slot_counts"].get(slot_label, 0) >= 3:
            return baseline["slot_thresholds"][slot_label], "3m_slot"
        return baseline["overall_threshold"], "3m_overall"
    return build_60m_fallback_threshold(bins_60m, config, now.hour), "60m_fallback"


def score_bin(bin_data: dict, threshold: float, config: ExplosionConfig) -> dict:
    tv = bin_data["trade_value"]
    ratio = tv / threshold if threshold > 0 else 0.0

    if ratio >= config.tier_high_ratio and bin_data["bullish"]:
        excess = min(ratio - config.tier_high_ratio, 1.0)
        score = config.score_high + excess * config.score_high_bonus_cap
        tier = "HIGH"
    elif ratio >= config.tier_watch_ratio:
        score = config.score_watch
        tier = "WATCH"
    else:
        score = 0.0
        tier = "NONE"

    return {"score": round(score, 1), "tier": tier, "ratio": round(ratio, 3),
            "trade_value": tv, "threshold": threshold}


def evaluate_closing_bet_candidate(
    today_bins: list[dict],
    baseline: dict | None,
    bins_60m_hist: list[dict],
    config: ExplosionConfig,
) -> dict:
    """장마감 임박(14:50) 시점 종가베팅 후보 평가.
    당일 3분봉(today_bins) 중 최근 5개(15분)를 score_bin()으로 채점해
    평균을 closing_score, 최대 ratio를 surge_ratio, 양봉 비율을 bullish_ratio로 집계.
    eligible = closing_score >= WATCH 기준 AND bullish_ratio >= 0.5."""
    recent = today_bins[-5:]
    if not recent:
        return {"eligible": False, "reason": "3분봉 데이터 없음"}

    scored = [
        score_bin(b, get_threshold_for_now(baseline, bins_60m_hist, config, b["dt"])[0], config)
        for b in recent
    ]
    closing_score = sum(s["score"] for s in scored) / len(scored)
    surge_ratio = max(s["ratio"] for s in scored)
    bullish_ratio = sum(1 for b in recent if b["bullish"]) / len(recent)

    return {
        "eligible": closing_score >= config.score_watch and bullish_ratio >= 0.5,
        "closing_score": round(closing_score, 1),
        "surge_ratio": round(surge_ratio, 3),
        "bullish_ratio": round(bullish_ratio, 3),
    }


class ExplosionPatternScorer:
    """종목당 하루 1회 이력 준비(prepare) -> 실시간 3분봉마다 score() 호출."""

    def __init__(self, config: ExplosionConfig | None = None):
        self.config = config or ExplosionConfig()
        self._cache: dict[str, dict] = {}

    def prepare(self, stock_code: str, bins_3m_hist: list[dict], bins_60m_hist: list[dict]) -> dict:
        today = datetime.now().date()
        cached = self._cache.get(stock_code)
        if cached and cached["date"] == today:
            return cached

        baseline = build_3m_baseline(bins_3m_hist, self.config)
        entry = {"date": today, "baseline": baseline, "bins_60m_hist": bins_60m_hist}
        self._cache[stock_code] = entry
        return entry

    def score(self, stock_code: str, latest_3m_bin: dict) -> dict:
        entry = self._cache.get(stock_code)
        if entry is None or entry["date"] != datetime.now().date():
            return {"score": 0.0, "tier": "UNPREPARED", "reason": "prepare() 먼저 호출 필요"}

        now = latest_3m_bin["dt"]
        threshold, source = get_threshold_for_now(entry["baseline"], entry["bins_60m_hist"], self.config, now)
        result = score_bin(latest_3m_bin, threshold, self.config)
        result["threshold_source"] = source
        return result