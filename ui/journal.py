"""매매일지 스냅샷 — 하루치를 파일로 굳힌다 (2026-08-11 신설).

왜 필요한가
-----------
DB는 매매 **기록**을 영구 보관하므로 과거 날짜 조회 자체는 언제든 된다.
문제는 **DB에 안 남는 것**이다:

  · 물타기 발동 횟수 — 추가매수는 기존 `trades` 행에 합쳐지고 `entry_reason`을
    덮지 않는다(뉴엔AI 실측). 유일한 흔적이 **로그**다.
  · 수동 추가매수 감지 / 확인 전용 차단 — 로그에만 남는다.
  · 그날의 체크리스트 판정 자체.

그런데 **로그는 10MB에서 로테이션되어 사라진다**(실측: 08-07은 14:30에
로테이션). 즉 며칠만 지나면 그날의 판정을 **원리적으로 재현할 수 없다.**
-> 장 마감 후 한 번, 로그가 살아있을 때 굳혀 둔다.

설계 원칙
---------
· **별도 프로세스**로 돈다(`daily_journal.py` + 작업 스케줄러). 봇 코드 0줄.
  이 프로젝트가 `premarket_scan.py`에서 이미 쓰는 패턴과 같다.
· **덮어쓰기 안전** — 같은 날 여러 번 돌려도 최신으로 갱신될 뿐이고,
  이미 있는 파일을 지우지 않는다(임시파일 -> os.replace 원자적 교체).
· **읽기 전용 소스** — DB는 SELECT만, 로그는 read만.
"""
from __future__ import annotations

import datetime as _dt
import json
import os

from . import checklist, queries as q

JOURNAL_DIR = os.path.join(q.PROJECT_ROOT, "observations", "journal")

SCHEMA_VERSION = 1


def path_for(date: str) -> str:
    return os.path.join(JOURNAL_DIR, f"{date.replace('-', '')}.json")


def build(date: str) -> dict:
    """그날 대시보드가 보여주는 전부를 한 덩어리로 만든다."""
    items = checklist.build(date)
    return {
        "schema": SCHEMA_VERSION,
        "date": date,
        "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "summary": q.day_summary(date),
        "checklist": items,
        "checklist_summary": checklist.summarize(items),
        "trades": q.trades(date),
        "exit_breakdown": q.exit_reason_breakdown(date),
        "watchlist": q.watchlist_summary(date),
        "events": q.system_events(date, limit=500),
        "holdings_at_save": q.holdings(),
        # 원자료도 같이 남긴다 — 나중에 판정 규칙이 바뀌어도 재계산할 수 있게.
        "log_counts": q.log_count(checklist._LOG_PATTERNS, date),
    }


def save(date: str) -> str:
    """스냅샷을 저장하고 경로를 돌려준다.

    임시파일에 쓰고 `os.replace`로 바꾼다 — 저장 중에 프로세스가 죽어도
    **기존 파일이 깨지지 않는다**(반쪽 JSON이 남으면 다음 로드가 통째로 실패한다).
    """
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    data = build(date)
    final = path_for(date)
    tmp = final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, final)
    return final


def load(date: str) -> dict | None:
    p = path_for(date)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def available() -> list[str]:
    """저장된 날짜 목록 (최신순)."""
    if not os.path.isdir(JOURNAL_DIR):
        return []
    out = []
    for name in os.listdir(JOURNAL_DIR):
        stem, ext = os.path.splitext(name)
        if ext == ".json" and len(stem) == 8 and stem.isdigit():
            out.append(f"{stem[:4]}-{stem[4:6]}-{stem[6:]}")
    return sorted(out, reverse=True)


def backfill(days: int = 30) -> list[str]:
    """거래가 있었던 과거 날짜를 한꺼번에 굳힌다.

    ⚠️ 과거분은 **로그가 이미 사라졌을 수 있다** — 그런 날은 체크리스트의
    로그 기반 항목이 `na`로 굳는다. 그게 정확한 상태이므로 그대로 저장한다
    ("모르면 모른다고 남긴다").
    """
    with q.ro_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT trade_date FROM trades
            WHERE trade_date >= CURRENT_DATE - %s::int
            ORDER BY trade_date DESC
            """,
            (days,),
        )
        dates = [r["trade_date"].isoformat() for r in cur.fetchall()]
    return [save(d) for d in dates]
