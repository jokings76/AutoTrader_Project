"""분봉 히스토리 영속 캐시 — 종가베팅 스캔의 REST 호출 절감 (2026-07-31 신규).

문제
────
종가베팅 스캐너(main.py task_closing_bet_scanner, 매일 14:50)는 대상 종목마다
20거래일치 3분봉/60분봉을 **매일 처음부터 다시** 받아왔다. 07-30 로그 실측
기준 종목당 5콜(3분봉 20일치 3콜 + 60분봉 1콜 + 당일분 1콜)이고, 이게 2분
안에 몰려서 나간다. 이 계정은 이미 429가 하루 2,469건 나는 포화 상태라
(07-30 기준 ka10080만 2,223건) 이 몰림이 전역 스로틀(0.6초)을 수십 초간
독점해 다른 REST 호출을 밀어낸다.

핵심 관찰: **어제까지의 과거 분봉은 절대 변하지 않는다.** 20일치를 매일 새로
받는 건 19일치를 낭비로 다시 받는 것과 같다.

해결
────
종목·주기별로 과거 분봉을 파일에 저장해두고, 매일 '캐시에 없는 최근 구간'만
1콜로 받아 이어붙인다. 캐시가 비었거나 너무 오래돼 구간이 이어지지 않으면
기존 방식(fetch_n_days_candles 전체 수집)으로 안전하게 되돌아간다.

절감(종목당, 캐시가 하루 지난 정상 상태 기준)
    3분봉 20일치   3콜 -> 1콜
    60분봉 20일치  1콜 -> 1콜
    당일 3분봉     1콜 -> 0콜  (3분봉 결과에서 잘라 쓰면 됨 — 호출부에서 처리)
    합계           5콜 -> 2콜  (60% 절감)

정렬 규약 (중요)
──────────────
이 모듈이 반환하는 캔들은 **항상 오래된 것 -> 최신 순(오름차순)** 이다.
history_fetcher.fetch_n_days_candles와 동일하고, KiwoomREST._raw_get_minute_candles
(최신 -> 과거, 내림차순)와는 **반대**다. 이 프로젝트에서 두 소스를 섞어 쓰다가
실제 버그가 났었다(종가베팅 today_bins[-5:]가 '최근 5개'를 의도했는데 내림차순
데이터라 '가장 오래된 5개'(전일 오후)를 채점하고 있었음 — 2026-07-31 발견).
그래서 이 모듈은 정렬을 한 곳에서 강제하고, 호출부는 항상 오름차순을 전제한다.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from utils.logger import logger

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "cache", "history")

# 증분 수집 시 요청할 캔들 수. 키움은 1응답 최대 900개를 주고(실측), 3분봉
# 900개면 약 8거래일치라 하루치 공백을 메우기에 충분하다. 주말/연휴로 며칠
# 비어도 커버된다. 이보다 크게 잡으면 페이징이 돌아 콜 수가 늘어난다.
INCREMENTAL_COUNT = 900

# 캐시 파일 포맷 버전 — 구조가 바뀌면 올려서 기존 캐시를 자동 무효화한다.
CACHE_VERSION = 1

_lock = threading.RLock()


def _cache_path(stock_code: str, interval: int) -> str:
    return os.path.join(CACHE_DIR, f"{interval}m", f"{stock_code}.json")


def _load(stock_code: str, interval: int) -> dict[str, dict]:
    """{time_str: candle} 반환. 없거나 손상됐으면 빈 dict."""
    path = _cache_path(stock_code, interval)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("version") != CACHE_VERSION:
            return {}
        return {c["time_str"]: c for c in payload.get("candles", [])}
    except Exception as e:
        logger.warning("[%s] 히스토리 캐시 로드 실패(%dm): %s", stock_code, interval, e)
        return {}


def _save(stock_code: str, interval: int, candles: list[dict]):
    path = _cache_path(stock_code, interval)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": CACHE_VERSION, "candles": candles}, f,
                      ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)  # 원자적 교체 — 쓰다 죽어도 기존 캐시가 안 깨짐
    except Exception as e:
        logger.warning("[%s] 히스토리 캐시 저장 실패(%dm): %s", stock_code, interval, e)


def _prune(by_time: dict[str, dict], target_days: int) -> list[dict]:
    """최근 target_days 거래일치만 남기고 오름차순 리스트로 반환."""
    dates = sorted({t[:8] for t in by_time})
    keep = set(dates[-target_days:])
    return sorted(
        (c for t, c in by_time.items() if t[:8] in keep),
        key=lambda c: c["time_str"],
    )


def fetch_with_cache(api, stock_code: str, interval: int = 3,
                     target_days: int = 20) -> list[dict]:
    """캐시를 활용해 target_days 거래일치 분봉을 반환 (오름차순).

    동작:
      1) 캐시를 읽는다.
      2) 캐시가 target_days를 채우고 있으면, 최근 구간 1콜만 받아 이어붙인다.
         받아온 구간의 가장 오래된 봉이 캐시 최신 날짜보다 이르거나 같으면
         두 구간이 겹치므로 공백 없이 이어진다.
      3) 캐시가 비었거나 공백이 생기면 기존 fetch_n_days_candles로 전체 수집.
      4) target_days로 잘라 저장하고 반환.

    실패 시에도 예외를 던지지 않고 최대한 가진 데이터를 돌려준다 —
    종가베팅은 하루 1회 부가 기능이라 여기서 죽어도 매매엔 영향이 없어야 한다.
    """
    from core.history_fetcher import fetch_n_days_candles

    with _lock:
        cached = _load(stock_code, interval)

    cached_dates = sorted({t[:8] for t in cached}) if cached else []
    need_full = len(cached_dates) < target_days

    if not need_full:
        newest_cached = cached_dates[-1]
        try:
            batch = api._raw_get_minute_candles(
                stock_code, interval=interval, count=INCREMENTAL_COUNT
            )
        except Exception as e:
            logger.warning("[%s] 증분 수집 실패(%dm), 캐시로 진행: %s",
                           stock_code, interval, e)
            batch = []

        if batch:
            oldest_new = min(c["time_str"] for c in batch)[:8]
            if oldest_new <= newest_cached:
                # 구간이 겹침 -> 공백 없이 이어붙일 수 있다
                merged = dict(cached)
                for c in batch:
                    merged[c["time_str"]] = c
                result = _prune(merged, target_days)
                with _lock:
                    _save(stock_code, interval, result)
                logger.info(
                    "[%s] 히스토리 증분 %dm: 캐시 %d일 + 신규 %d봉 -> %d봉 (1콜)",
                    stock_code, interval, len(cached_dates), len(batch), len(result),
                )
                return result
            logger.info(
                "[%s] 히스토리 %dm 공백 감지(캐시 최신 %s < 신규 최古 %s) -> 전체 재수집",
                stock_code, interval, newest_cached, oldest_new,
            )
        need_full = True

    # 전체 수집 (첫날 / 공백 / 캐시 손상)
    full = fetch_n_days_candles(api, stock_code, interval=interval,
                                target_days=target_days)
    if not full:
        # 새로 못 받았으면 기존 캐시라도 반환(끊김 방지)
        return _prune(cached, target_days) if cached else []

    merged = dict(cached)
    for c in full:
        merged[c["time_str"]] = c
    result = _prune(merged, target_days)
    with _lock:
        _save(stock_code, interval, result)
    return result


def slice_today(candles: list[dict], today: str | None = None) -> list[dict]:
    """오름차순 캔들에서 당일분만 잘라 반환 (오름차순 유지).

    종가베팅의 '당일 3분봉' 전용 호출(count=150)을 없애기 위한 헬퍼.
    그 호출은 20일치 3분봉에 이미 포함된 데이터를 다시 받아오는 중복이었고,
    게다가 내림차순이라 today_bins[-5:]가 '최근 5봉'이 아니라 '가장 오래된
    5봉(전일 오후)'을 가리키는 버그까지 있었다. 여기서 오름차순으로 당일만
    잘라 주면 두 문제가 같이 해결된다."""
    today = today or datetime.now().strftime("%Y%m%d")
    return [c for c in candles if c.get("time_str", "")[:8] == today]


def cache_stats() -> dict:
    """캐시 현황 (진단용)."""
    if not os.path.isdir(CACHE_DIR):
        return {"files": 0, "bytes": 0}
    files = bytes_ = 0
    for root, _, names in os.walk(CACHE_DIR):
        for n in names:
            if n.endswith(".json"):
                files += 1
                bytes_ += os.path.getsize(os.path.join(root, n))
    return {"files": files, "bytes": bytes_}
