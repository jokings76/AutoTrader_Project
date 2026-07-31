"""개장초반 틱 아카이브 — 1A(evaluate_1a_leading_strength) 백테스트용 (2026-07-31 신규).

배경
────
1A는 체결강도(TradeFlowTracker.compute_strength)를 틱 단위로 판정하는데, 지금
백테스트(daily_backtest.py)는 1분봉만 있어서 이 부분을 재현할 수 없다.
키움 REST에 ka10079(주식틱차트조회요청)가 있어 시각+가격+거래량 단위의 진짜
개별 체결 데이터를 받을 수 있지만(대신증권 없이 지금 계정으로 바로 가능),
매수/매도 구분 필드는 없어서 틱룰(Tick Rule — 직전 체결가보다 오르면 매수
우세/내리면 매도 우세로 추정, 시장미시구조 분석의 표준 근사법)로 보완한다.

스코프를 줄인 이유
──────────────
종목당 하루 전체 틱을 받으려면 실측 결과 종목당 22~185회 REST 호출이
필요했다(2026-07-31 실측, 429 예산 이미 포화 상태라 무리). 1A/1L이 실제로
판단하는 구간은 장 시작 직후뿐이므로, 하루 전체 대신 개장초반 윈도우
(기본 09:00~09:15)만 받는다 — 종목당 호출 수가 크게 줄고, 정확히 검증하고
싶은 구간이기도 하다.

타이밍이 중요함(반드시 인지할 것)
────────────────────────
ka10079는 '현재 시각'에서부터 과거로 페이지네이션한다(cont-yn/next-key,
응답 헤더에 있음 — body가 아님, _request()가 헤더를 버리므로 여기선 자체
페이징한다). 그래서:
  - 09:15~09:20 사이(윈도우가 끝난 직후)에 실행하면 몇 페이지 안에 09:00까지
    도달해서 저렴하다(종목당 1~7콜 수준, 2026-07-31 실측 비율로 역산).
  - 장 마감 후(예: 지금처럼 15:30 이후)에 실행하면 '현재'가 15:30이라
    09:00~09:15까지 페이징해 내려가는 데 하루 전체를 받는 것과 비슷한
    비용이 든다(종목당 최대 185콜) — 당일 소급 수집은 비싸다는 뜻.
  - 그래서 이 모듈은 매일 09:20 직후 실행하도록 설계됐다(main.py에 태스크로
    등록하는 건 별도 작업, 지금은 독립 스크립트로 실행).

저장
────
cache/tick_history/{YYYYMMDD}/{code}.json — history_cache.py와 같은 관례
(원자적 저장, version 필드로 포맷 변경 시 자동 무효화). 정렬은 오름차순
(과거->최신)으로 통일 — 이 프로젝트의 정렬 규약과 일치.
"""
from __future__ import annotations

import json
import os
import time as time_module
from datetime import date, datetime

import requests

from utils.logger import logger

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICK_CACHE_DIR = os.path.join(BASE_DIR, "cache", "tick_history")
CACHE_VERSION = 1

WINDOW_START_HHMM = "0900"
WINDOW_END_HHMM = "0915"
MAX_PAGES = 30          # 안전 상한 (30 x 900 = 27,000틱) — 장마감후 소급수집 대비 여유있게
PAGE_SLEEP_SEC = 1.0    # 429 예방 (이 계정은 이미 포화 상태로 실측됨)


def _cache_path(trade_date: str, stock_code: str) -> str:
    return os.path.join(TICK_CACHE_DIR, trade_date, f"{stock_code}.json")


def _save(trade_date: str, stock_code: str, ticks: list[dict]):
    path = _cache_path(trade_date, stock_code)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"version": CACHE_VERSION, "ticks": ticks},
                f, ensure_ascii=False, separators=(",", ":"),
            )
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("[%s] 틱 아카이브 저장 실패: %s", stock_code, e)


def load_ticks(trade_date: str, stock_code: str) -> list[dict]:
    """저장된 틱 로드 (오름차순). 없으면 빈 리스트."""
    path = _cache_path(trade_date, stock_code)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("version") != CACHE_VERSION:
            return []
        return payload.get("ticks", [])
    except Exception as e:
        logger.warning("[%s] 틱 아카이브 로드 실패: %s", stock_code, e)
        return []


def _fetch_raw_ticks(
    host: str, token: str, stock_code: str,
    window_start_hhmm: str, window_end_hhmm: str,
    max_pages: int = MAX_PAGES,
) -> tuple[list[dict], int]:
    """ka10079를 헤더 기반으로 직접 페이징해서 윈도우 안의 틱만 수집.
    반환: (윈도우 내 원시 행 리스트(내림차순 그대로), 사용한 페이지 수)."""
    url = f"{host}/api/dostk/chart"
    body = {"stk_cd": stock_code, "tic_scope": "1", "upd_stkpc_tp": "1"}
    window_rows: list[dict] = []
    cont_yn, next_key = "N", ""
    pages = 0

    for _ in range(max_pages):
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "cont-yn": cont_yn,
            "next-key": next_key,
            "api-id": "ka10079",
        }
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        pages += 1
        try:
            data = res.json()
        except Exception as e:
            logger.warning("[%s] ka10079 응답 파싱 실패: %s", stock_code, e)
            break
        if data.get("return_code") != 0:
            logger.warning(
                "[%s] ka10079 실패 code=%s msg=%s",
                stock_code, data.get("return_code"), data.get("return_msg"),
            )
            break

        rows = data.get("stk_tic_chart_qry", [])
        if not rows:
            break

        for row in rows:
            tm = row.get("cntr_tm", "")
            if len(tm) != 14:
                continue
            hhmm = tm[8:12]
            if window_start_hhmm <= hhmm <= window_end_hhmm:
                window_rows.append(row)

        oldest_hhmm = rows[-1].get("cntr_tm", "")[8:12]
        if oldest_hhmm and oldest_hhmm < window_start_hhmm:
            # 이 페이지가 윈도우 시작보다 더 과거까지 내려갔음 -> 더 받을 필요 없음
            break

        if res.headers.get("cont-yn") == "Y" and res.headers.get("next-key"):
            cont_yn = "Y"
            next_key = res.headers.get("next-key")
            time_module.sleep(PAGE_SLEEP_SEC)
        else:
            break

    return window_rows, pages


def _apply_tick_rule(rows_ascending: list[dict]) -> list[dict]:
    """오름차순 원시 행에 틱룰 적용해서 add_tick() 형식으로 변환.
    틱룰: 직전 체결가보다 오르면 buy, 내리면 sell, 같으면 직전 방향 유지
    (첫 틱은 방향을 알 수 없어 neutral)."""
    result = []
    prev_price = None
    prev_side = "neutral"
    for row in rows_ascending:
        price = int(row["cur_prc"])
        volume = int(row["trde_qty"])
        tm = row["cntr_tm"]
        if prev_price is None:
            side = "neutral"
        elif price > prev_price:
            side = "buy"
        elif price < prev_price:
            side = "sell"
        else:
            side = prev_side
        result.append({
            "time_str": tm,
            "price": price,
            "volume": volume,
            "side": side,
        })
        prev_price = price
        prev_side = side
    return result


def archive_stock(
    host: str, token: str, stock_code: str, trade_date: str | None = None,
    window_start_hhmm: str = WINDOW_START_HHMM,
    window_end_hhmm: str = WINDOW_END_HHMM,
) -> int:
    """한 종목의 개장초반 틱을 받아 틱룰 적용 후 저장. 반환: 저장된 틱 개수."""
    trade_date = trade_date or date.today().strftime("%Y%m%d")
    raw_rows, pages = _fetch_raw_ticks(
        host, token, stock_code, window_start_hhmm, window_end_hhmm
    )
    if not raw_rows:
        logger.info("[%s] 틱 아카이브: 윈도우 내 데이터 없음 (%d페이지 조회)", stock_code, pages)
        return 0

    ascending = sorted(raw_rows, key=lambda r: r["cntr_tm"])
    ticks = _apply_tick_rule(ascending)
    _save(trade_date, stock_code, ticks)
    logger.info(
        "[%s] 틱 아카이브 완료: %d틱 (%s~%s), %d페이지 호출",
        stock_code, len(ticks), ticks[0]["time_str"], ticks[-1]["time_str"], pages,
    )
    return len(ticks)


def archive_universe(
    host: str, token: str, stock_codes: list[str], trade_date: str | None = None,
    window_start_hhmm: str = WINDOW_START_HHMM,
    window_end_hhmm: str = WINDOW_END_HHMM,
    sleep_between_stocks_sec: float = 1.0,
) -> dict[str, int]:
    """여러 종목을 순회하며 아카이빙. 반환: {code: 저장된틱수}."""
    results = {}
    for code in stock_codes:
        try:
            results[code] = archive_stock(
                host, token, code, trade_date, window_start_hhmm, window_end_hhmm
            )
        except Exception as e:
            logger.warning("[%s] 틱 아카이브 실패: %s", code, e)
            results[code] = -1
        time_module.sleep(sleep_between_stocks_sec)
    return results


if __name__ == "__main__":
    from api.auth import get_access_token
    from config import settings
    import core.daily_backtest as bt

    token = get_access_token()
    host = "https://mockapi.kiwoom.com" if settings.IS_MOCK else "https://api.kiwoom.com"

    universe = bt._get_today_universe()
    codes = [row["stock_code"] for row in universe]
    print(f"대상 {len(codes)}종목: {codes}")

    results = archive_universe(host, token, codes)
    ok = sum(1 for v in results.values() if v > 0)
    print(f"완료: {ok}/{len(codes)}종목 성공")
    for code, n in results.items():
        print(f"  {code}: {n}틱")
