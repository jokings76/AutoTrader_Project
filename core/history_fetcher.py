"""
KiwoomREST.get_minute_candles / _raw_get_minute_candles 재사용한 N거래일치 히스토리 수집.

핵심 발견 (2026-07-26 검증):
  - _raw_get_minute_candles(base_date=X)는 X일 기준으로 최대 900개(약 7거래일)를
    반환하고, 응답의 가장 오래된 time_str 날짜를 다음 호출의 base_date로 넘기면
    그 이전 구간이 이어서 나온다 (경계일 1개 중복 -> dedupe 필요).
  - get_minute_candles()의 캐시 래퍼는 base_date 없이 호출 시 max_loops=10 제한이
    있어 한 번에 20일치를 못 채우므로, 이 모듈에서 직접 base_date를 갱신하며
    여러 번 호출한다.
"""

from utils.logger import logger


def fetch_n_days_candles(api, stock_code: str, interval: int = 3,
                           target_days: int = 20, max_calls: int = 8) -> list[dict]:
    """base_date를 뒤로 이동시키며 반복 호출해 target_days 거래일치 캔들 수집.
    반환: time_str 기준 오름차순(과거->최신) 정렬, 중복 제거된 캔들 리스트.
    각 항목: {"time_str": "YYYYMMDDHHMMSS", "open":, "high":, "low":, "close":, "volume":}
    """
    all_candles: dict[str, dict] = {}  # time_str -> candle (자동 dedupe)
    base_date = None
    calls = 0

    while calls < max_calls:
        calls += 1
        try:
            batch = api._raw_get_minute_candles(
                stock_code, interval=interval, count=2600, base_date=base_date
            )
        except Exception as e:
            logger.error(f"[{stock_code}] 히스토리 배치 수집 실패 (call {calls}): {e}")
            break

        if not batch:
            logger.info(f"[{stock_code}] 더 이상 과거 데이터 없음 (call {calls})")
            break

        for c in batch:
            all_candles[c["time_str"]] = c

        unique_dates = {t[:8] for t in all_candles.keys()}
        oldest_time = min(batch, key=lambda c: c["time_str"])["time_str"]
        oldest_date = oldest_time[:8]

        logger.info(
            f"[{stock_code}] 히스토리 call {calls}: 누적 {len(all_candles)}개, "
            f"누적 {len(unique_dates)}일치 (최근 base_date={oldest_date})"
        )

        if len(unique_dates) >= target_days:
            break
        if oldest_date == base_date:
            # 진전 없음 (더 이상 과거 데이터 없음)
            break
        base_date = oldest_date

    result = sorted(all_candles.values(), key=lambda c: c["time_str"])
    logger.info(f"[{stock_code}] 히스토리 수집 완료: 캔들 {len(result)}개")
    return result


def to_trade_value_bins(candles: list[dict]) -> list[dict]:
    """get_minute_candles 반환 형식({time_str, open, high, low, close, volume})을
    explosion_scorer가 쓰는 bin 형식({dt, trade_value, bullish, ...})으로 변환.
    거래대금 필드가 없으므로 close * volume으로 근사."""
    from datetime import datetime

    bins = []
    for c in candles:
        try:
            dt = datetime.strptime(c["time_str"], "%Y%m%d%H%M%S")
        except (ValueError, KeyError):
            continue
        close = c.get("close") or 0
        volume = c.get("volume") or 0
        open_ = c.get("open") or close
        bins.append({
            "bin_key": c["time_str"][:12],
            "dt": dt,
            "open": open_, "high": c.get("high"), "low": c.get("low"), "close": close,
            "volume": volume,
            "trade_value": close * volume,
            "bullish": close > open_,
        })
    return bins