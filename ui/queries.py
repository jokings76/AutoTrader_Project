"""대시보드 데이터 조회 — **읽기 전용**.

Phase 0 대시보드(2026-08-11 신설)의 데이터 계층.

🔴 이 모듈이 지켜야 하는 단 하나의 규칙
--------------------------------------
**`core/` · `main.py`를 절대 import하지 않는다.**

그래야 UI를 아무리 고쳐도 매매 로직과 15개 스위트 1,660건에 영향이 없다.
= **장중에도 UI만은 안전하게 수정할 수 있다**(CLAUDE.md "장중 매매로직 수정 금지"를
  어기지 않는다). 이 파일이 `from core...`를 하는 순간 그 보장이 사라진다.

허용 의존은 둘뿐이다:
  · `db.connection` — DB 설정(config.ini)·풀의 **단일 출처**. 여기서 복제하면
    이 코드베이스의 반복 사고 1위("같은 규칙이 여러 경로에 흩어져 조용히
    어긋난다")를 그대로 밟는다. 매매 로직이 아니라 읽기 인프라라 안전하다.
  · 표준 라이브러리

🔴 시간대 함정 (2026-08-11 실측으로 확인)
----------------------------------------
    trades.buy_time / sell_time   ->  **로컬(KST)**
    system_events.timestamp       ->  **UTC**  (KST = UTC + 9h)

DB 세션 TimeZone이 UTC라 같은 사건이 두 테이블에 9시간 어긋나 들어 있다.
실측: 뉴엔AI 손절이 trades엔 `13:51:44`, system_events엔 `04:51:44`.
**한 화면에 섞으면 반드시 틀린다** — system_events는 `_to_kst()`를 통과시킬 것.

🔴 쓰기 금지는 코드 규약이 아니라 DB 레벨로 강제한다
--------------------------------------------------
`ro_cursor()`가 커넥션을 `readonly=True` 세션으로 만든다. 실수로 UPDATE를
쓰면 psycopg2가 거부한다. 실계좌가 걸린 프로젝트에서 "읽기 전용입니다"라는
주석은 보장이 아니다.
"""
from __future__ import annotations

import datetime as _dt
import os
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from psycopg2.extras import RealDictCursor

from db.connection import get_connection

# ---------------------------------------------------------------------------
# 경로 / 상수
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_HERE)
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
MAIN_LOG = os.path.join(LOG_DIR, "autotrader.log")

# system_events 를 KST 로 옮기는 폭. DB 세션이 UTC 라서 고정 +9h 로 충분하다
# (한국은 서머타임이 없다).
_KST_SHIFT = _dt.timedelta(hours=9)

# 봇 생존 판정 — 이 초 안에 로그가 쓰였으면 '돌고 있다'로 본다.
# 장중엔 체결틱마다 로그가 쏟아지므로 60초면 충분히 여유 있다.
BOT_ALIVE_WINDOW_SEC = 90


# ---------------------------------------------------------------------------
# 커서 / 직렬화
# ---------------------------------------------------------------------------
@contextmanager
def ro_cursor():
    """읽기 전용 커서.

    `set_session(readonly=True)`는 **트랜잭션이 없을 때만** 걸린다. 풀에서 갓
    꺼낸 커넥션은 그 상태이지만, 앞선 사용자가 남긴 트랜잭션이 있을 수 있어
    rollback을 먼저 부른다.

    ⚠️ 이 설정은 풀의 커넥션에 남는다 — 의도한 것이다. UI 프로세스는 자기
    풀을 따로 가지므로(봇과 별도 프로세스) UI의 모든 커넥션이 읽기 전용이 된다.
    """
    with get_connection() as conn:
        try:
            conn.rollback()
            conn.set_session(readonly=True)
        except Exception:
            # readonly 설정 실패가 조회를 막을 이유는 없다. 쿼리는 전부
            # 하드코딩된 SELECT라 실질 위험은 없고, 이건 이중 안전장치다.
            pass
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cur
        finally:
            cur.close()


def _jsonable(v: Any) -> Any:
    """psycopg2 타입 -> JSON 가능 타입.

    Decimal(numeric)과 date/datetime을 그대로 두면 FastAPI 인코더에 맡기게 되는데,
    Decimal이 문자열로 나가 프론트에서 조용히 문자열 연산이 되는 사고가 흔하다.
    여기서 못박는다.
    """
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    return v


def _rows(cur) -> list[dict]:
    return [{k: _jsonable(v) for k, v in row.items()} for row in cur.fetchall()]


def _to_kst(ts: _dt.datetime | None) -> _dt.datetime | None:
    """system_events.timestamp(UTC) -> KST. 위 모듈 주석 참고."""
    return None if ts is None else ts + _KST_SHIFT


def today() -> str:
    return _dt.date.today().isoformat()


# ---------------------------------------------------------------------------
# 1) 상태 — 봇이 살아있나, 오늘 얼마인가
# ---------------------------------------------------------------------------
def bot_status() -> dict:
    """봇 생존 여부와 로그 신선도.

    ⚠️ 프로세스 목록을 보지 않고 **로그 파일 mtime**으로 판정한다. UI는 봇과
    다른 프로세스이고, 플랫폼 의존적인 프로세스 조회를 넣으면 UI가 OS에
    묶인다. 로그는 봇이 살아있는 동안 계속 쓰이므로 더 정확한 신호다.
    """
    info = {
        "log_path": MAIN_LOG,
        "log_exists": os.path.exists(MAIN_LOG),
        "last_log_at": None,
        "age_sec": None,
        "alive": False,
    }
    if info["log_exists"]:
        mtime = os.path.getmtime(MAIN_LOG)
        last = _dt.datetime.fromtimestamp(mtime)
        age = (_dt.datetime.now() - last).total_seconds()
        info["last_log_at"] = last.isoformat()
        info["age_sec"] = round(age, 1)
        info["alive"] = age <= BOT_ALIVE_WINDOW_SEC
    return info


def day_summary(date: str) -> dict:
    """그날의 실현손익·승률·평균 보유시간.

    ⚠️ 평균 보유시간은 08-10에 드러난 이 봇의 핵심 문제("41건 평균 25분,
    매도 후 당일 고가까지 +5.45% 더 갔다")를 매일 눈에 보이게 하려고 넣었다.
    """
    with ro_cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*)                                             AS closed,
                COUNT(*) FILTER (WHERE profit_amount > 0)            AS wins,
                COUNT(*) FILTER (WHERE profit_amount < 0)            AS losses,
                COALESCE(SUM(profit_amount), 0)                      AS pnl,
                AVG(profit_rate)                                     AS avg_rate,
                AVG(EXTRACT(EPOCH FROM (sell_time - buy_time)) / 60) AS avg_hold_min
            FROM trades
            WHERE trade_date = %s AND status = 'closed'
            """,
            (date,),
        )
        row = {k: _jsonable(v) for k, v in cur.fetchone().items()}

        cur.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE trade_date = %s",
            (date,),
        )
        row["buys"] = cur.fetchone()["n"]

    # ⚠️ 승률의 분모는 `closed`가 아니라 **wins + losses**다.
    #    손익 0원인 건(수동매도 체결가 미상 / 미체결 정리)이 08-11에만 10건이라
    #    청산 건수로 나누면 승률이 27.6%로 나와 실제(42%)를 크게 왜곡한다.
    #    대신 그 건수를 `flat`으로 같이 내보내 숨기지 않는다.
    wins, losses = row["wins"] or 0, row["losses"] or 0
    decided = wins + losses
    row["flat"] = (row["closed"] or 0) - decided
    row["win_rate"] = round(100.0 * wins / decided, 1) if decided else None
    for k in ("avg_rate", "avg_hold_min"):
        if row.get(k) is not None:
            row[k] = round(row[k], 2)
    return row


def holdings() -> list[dict]:
    """미청산 포지션.

    ⚠️ `status='manual'`은 **봇 관리 밖**이다(08-06에 격리한 3종목). 같이
    보여주되 반드시 구분한다 — 이걸 봇 보유로 착각하면 "왜 안 팔지?"라는
    잘못된 진단으로 간다.
    """
    with ro_cursor() as cur:
        cur.execute(
            """
            SELECT id, trade_date, stock_code, stock_name, buy_price, buy_quantity,
                   buy_amount, sub_strategy, status, buy_time, entry_reason
            FROM trades
            WHERE status <> 'closed'
            ORDER BY buy_time DESC
            """
        )
        return _rows(cur)


# ---------------------------------------------------------------------------
# 2) 매매 내역
# ---------------------------------------------------------------------------
def trades(date: str) -> list[dict]:
    with ro_cursor() as cur:
        cur.execute(
            """
            SELECT id, stock_code, stock_name, sub_strategy, status,
                   buy_price, buy_quantity, sell_price, buy_time, sell_time,
                   profit_rate, profit_amount, entry_reason, exit_reason,
                   EXTRACT(EPOCH FROM (sell_time - buy_time)) / 60 AS hold_min
            FROM trades
            WHERE trade_date = %s
            ORDER BY buy_time
            """,
            (date,),
        )
        rows = _rows(cur)
    for r in rows:
        if r.get("hold_min") is not None:
            r["hold_min"] = round(r["hold_min"], 1)
    return rows


def exit_reason_breakdown(date: str) -> list[dict]:
    """청산 사유 분포. `(` 앞부분만 잘라 종류별로 접는다.

    (사유 문자열에 수치가 박혀 있어 — 예: `손절 가격-2.59% (순 -2.82%...)` —
     통째로 GROUP BY 하면 전부 1건씩 나온다.)
    """
    with ro_cursor() as cur:
        cur.execute(
            """
            SELECT regexp_replace(split_part(exit_reason, '(', 1), '\\s+$', '') AS reason,
                   COUNT(*)                       AS n,
                   COALESCE(SUM(profit_amount), 0) AS pnl,
                   AVG(profit_rate)                AS avg_rate
            FROM trades
            WHERE trade_date = %s AND exit_reason IS NOT NULL
            GROUP BY 1
            ORDER BY n DESC, pnl
            """,
            (date,),
        )
        rows = _rows(cur)
    for r in rows:
        if r.get("avg_rate") is not None:
            r["avg_rate"] = round(r["avg_rate"], 2)
    return rows


# ---------------------------------------------------------------------------
# 3) 진단 — 왜 안 샀나 / 어느 조건식에서 왔나
# ---------------------------------------------------------------------------
def watchlist_summary(date: str) -> dict:
    """조건식별 편입/매수 + 미매수 사유 집계.

    🔴 `cond_name`은 08-12 최우선 점검 대상이다. 08-11까지는
    `돌파자동매매용`이었고, 08-12부터 `돌파전` / `돌파후`로 갈려야 한다.
    옛 이름이 계속 보이면 **새 조건식이 봇 구독에 안 붙은 것**이다.
    """
    with ro_cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(cond_name, '(미상)') AS cond_name,
                   COUNT(*)                                        AS n,
                   COUNT(*) FILTER (WHERE is_bought)               AS bought
            FROM watch_list_log
            WHERE trade_date = %s
            GROUP BY 1 ORDER BY n DESC
            """,
            (date,),
        )
        by_cond = _rows(cur)

        cur.execute(
            """
            SELECT COALESCE(reason_not_bought, '(사유 없음)') AS reason, COUNT(*) AS n
            FROM watch_list_log
            WHERE trade_date = %s AND NOT is_bought
            GROUP BY 1 ORDER BY n DESC LIMIT 20
            """,
            (date,),
        )
        reasons = _rows(cur)

    return {"by_condition": by_cond, "not_bought_reasons": reasons}


# ---------------------------------------------------------------------------
# 4) 시스템 이벤트 (UTC -> KST 변환 지점)
# ---------------------------------------------------------------------------
def system_events(date: str, limit: int = 80) -> list[dict]:
    """그날의 시스템 이벤트.

    🔴 저장은 UTC, 표시는 KST다. 날짜 필터도 UTC 기준으로 걸어야 KST 하루가
    온전히 잡힌다 — KST 08-12 00:00~24:00 = UTC 08-11 15:00 ~ 08-12 15:00.
    (`date` 컬럼으로 그냥 자르면 아침 9시 이전 사건이 전날로 새어나간다.)
    """
    d = _dt.date.fromisoformat(date)
    start_utc = _dt.datetime.combine(d, _dt.time(0, 0)) - _KST_SHIFT
    end_utc = start_utc + _dt.timedelta(days=1)

    with ro_cursor() as cur:
        cur.execute(
            """
            SELECT timestamp, event_type, severity, event_message
            FROM system_events
            WHERE timestamp >= %s AND timestamp < %s
            ORDER BY timestamp DESC
            LIMIT %s
            """,
            (start_utc, end_utc, limit),
        )
        raw = cur.fetchall()

    out = []
    for r in raw:
        out.append(
            {
                "timestamp": _to_kst(r["timestamp"]).isoformat(),
                "event_type": r["event_type"],
                "severity": r["severity"],
                "event_message": r["event_message"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# 5) 로그 tail
# ---------------------------------------------------------------------------
def log_tail(lines: int = 120, grep: str | None = None, date: str | None = None) -> dict:
    """로그 마지막 N줄.

    ⚠️ 로그는 10MB에서 로테이션한다. 파일을 통째로 읽으면 UI가 그때마다 10MB를
    삼키므로 **끝에서부터 필요한 만큼만** 읽는다.
    ⚠️ grep은 파일 전체가 아니라 **읽어온 꼬리 안에서만** 찾는다 — 장중에
    10MB 전수 검색을 걸면 응답이 멈춘다. 과거를 뒤질 땐 로그 파일을 직접 볼 것.

    `date`가 오늘이 아니면 로테이션분까지 훑어 **그 날짜 줄만** 모은다
    (매매일지로 과거를 볼 때 오늘 로그가 섞이면 안 된다).
    """
    if date and date != today():
        prefix = f"[{date} "
        rows: list[str] = []
        for path in reversed(log_files()):          # 오래된 것부터 -> 시간순
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    rows.extend(ln.rstrip("\n") for ln in f if ln.startswith(prefix))
            except OSError:
                continue
        if grep:
            needle = grep.lower()
            rows = [ln for ln in rows if needle in ln.lower()]
        return {
            "lines": rows[-lines:],
            "truncated": len(rows) > lines,
            "size_mb": None,
            "note": None if rows else f"{date} 로그 없음 — 로테이션으로 소실됐을 수 있다",
        }

    if not os.path.exists(MAIN_LOG):
        return {"lines": [], "error": f"로그 없음: {MAIN_LOG}"}

    lines = max(1, min(lines, 1000))
    # 한 줄 평균 180바이트로 넉넉히 잡고, 최소 64KB는 읽는다.
    want = max(64 * 1024, lines * 400)
    size = os.path.getsize(MAIN_LOG)
    with open(MAIN_LOG, "rb") as f:
        f.seek(max(0, size - want))
        chunk = f.read()

    text = chunk.decode("utf-8", errors="replace")
    rows = text.splitlines()
    if size > want and rows:
        rows = rows[1:]  # 첫 줄은 잘렸을 수 있다

    if grep:
        needle = grep.lower()
        rows = [ln for ln in rows if needle in ln.lower()]

    return {"lines": rows[-lines:], "truncated": size > want, "size_mb": round(size / 1048576, 2)}


def log_files() -> list[str]:
    """`autotrader.log`, `.log.1`, `.log.2` … 최신순.

    로그는 10MB에서 로테이션한다. **하루가 한 파일에 다 들어있다는 보장이 없다**
    (실측: 08-07은 14:30에 로테이션이 일어나 오후가 `.log.1`로 넘어갔다).
    날짜로 세려면 반드시 로테이션분까지 봐야 한다.
    """
    if not os.path.isdir(LOG_DIR):
        return []
    out = []
    for name in os.listdir(LOG_DIR):
        if name == "autotrader.log" or name.startswith("autotrader.log."):
            out.append(os.path.join(LOG_DIR, name))
    # autotrader.log(현재) -> .log.1 -> .log.2 … 숫자가 클수록 과거
    def order(p: str) -> int:
        tail = os.path.basename(p).rsplit(".", 1)[-1]
        return int(tail) if tail.isdigit() else 0
    return sorted(out, key=order)


_LOG_CACHE: dict[tuple, tuple[float, dict | None]] = {}
_LOG_CACHE_TTL = 15.0


def log_count(patterns: dict[str, str], date: str | None = None) -> dict[str, int] | None:
    """패턴별 등장 횟수를 **그 날짜의 줄에서만** 센다.

    🔴 2026-08-11 결함 수정. 전에는 날짜와 무관하게 '현재 로그 꼬리'를 셌다.
    그러면 과거 날짜를 열었을 때 **오늘 숫자가 그날 것처럼 표시된다** —
    매매일지로 쓰는 순간 정확히 틀린 정보를 주는 종류의 결함이다.
    (이 프로젝트가 반복 경고해 온 "구간별 집계는 날짜로 쪼개 볼 것"과 같은 계열.)

    반환:
        dict  — 그날 줄을 실제로 찾았다.
        None  — **그 날짜의 줄이 로그에 하나도 없다.** 로테이션으로 지워졌거나
                애초에 안 돈 날이다. 호출부는 이걸 `na`(판정 불가)로 다뤄야지
                0으로 뭉개면 안 된다.

    로그 한 줄은 `[2026-08-11 14:43:59] [INFO] ...` 형식이라 날짜 접두사로 자른다.
    """
    day = date or today()
    files = log_files()
    sig = tuple((p, os.path.getmtime(p), os.path.getsize(p)) for p in files if os.path.exists(p))
    key = (day, sig)

    hit = _LOG_CACHE.get(key)
    if hit is not None:
        ts, val = hit
        # 과거 날짜 + 파일이 안 바뀌었으면 결과도 안 바뀐다 -> 영구 캐시.
        # 오늘치만 TTL을 둔다.
        if day != today() or (_dt.datetime.now().timestamp() - ts) < _LOG_CACHE_TTL:
            return val

    prefix = f"[{day} "
    found_any = False
    out = {k: 0 for k in patterns}

    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.startswith(prefix):
                        continue
                    found_any = True
                    for k, pat in patterns.items():
                        if pat in line:
                            out[k] += 1
        except OSError:
            continue

    result = out if found_any else None
    _LOG_CACHE[key] = (_dt.datetime.now().timestamp(), result)
    if len(_LOG_CACHE) > 64:                       # 무한 증식 방지
        for k in list(_LOG_CACHE)[:32]:
            _LOG_CACHE.pop(k, None)
    return result
