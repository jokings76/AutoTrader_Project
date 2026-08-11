"""AutoTrader 대시보드 서버 — Phase 0 (읽기 전용).

실행:
    python -m ui.server          (또는 run_dashboard.bat)
    -> http://127.0.0.1:8787

🔴 안전 설계
------------
1. **읽기 전용.** 주문·상수변경·DB쓰기 엔드포인트가 **하나도 없다.**
   DB 커넥션도 `readonly=True` 세션이라 실수로 UPDATE를 써도 DB가 거부한다.
2. **루프백 전용.** 기본 바인딩이 `127.0.0.1`이다. 실계좌가 걸린 화면을
   LAN에 노출하지 않는다. 바꾸려면 `AUTOTRADER_UI_HOST` 환경변수를 명시적으로
   줘야 하고, 그때 경고를 크게 찍는다.
3. **봇과 별도 프로세스.** UI가 죽어도 봇은 살고, 봇이 죽어도 UI로 원인을
   본다(`start_trader.bat`이 원격제어를 먼저 띄우는 것과 같은 이유).
4. **예외를 삼키지 않되 페이지를 죽이지 않는다.** DB가 내려가도 각 패널이
   자기 자리에서 에러를 표시할 뿐 전체 화면이 백지가 되지 않는다.

⚠️ 장중에 이 파일을 고쳐도 매매에 영향이 없다 — `core/`를 import하지 않으므로
   15개 스위트 1,660건과 완전히 분리돼 있다. 그게 Phase 0의 설계 목표다.
"""
from __future__ import annotations

import logging
import os
import sys
import traceback

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse

from . import checklist, journal, queries as q

logger = logging.getLogger("ui.server")

HOST = os.environ.get("AUTOTRADER_UI_HOST", "127.0.0.1")
PORT = int(os.environ.get("AUTOTRADER_UI_PORT", "8787"))

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_INDEX = os.path.join(_STATIC, "index.html")

app = FastAPI(
    title="AutoTrader 대시보드",
    description="Phase 0 — 읽기 전용 관측. 주문·설정변경 기능 없음.",
    docs_url="/api/docs",
)


def _safe(fn, *args, **kwargs):
    """패널 하나가 실패해도 화면 전체를 죽이지 않는다."""
    try:
        return {"ok": True, "data": fn(*args, **kwargs)}
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 화면엔 사유를 보여준다
        logger.warning("조회 실패 %s: %s", getattr(fn, "__name__", fn), e)
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc(limit=3),
        }


# ---------------------------------------------------------------------------
# 페이지
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    if not os.path.exists(_INDEX):
        return JSONResponse({"error": f"index.html 없음: {_INDEX}"}, status_code=500)
    return FileResponse(_INDEX)


# ---------------------------------------------------------------------------
# API — 전부 GET, 전부 읽기 전용
# ---------------------------------------------------------------------------
@app.get("/api/overview")
def api_overview(date: str | None = Query(None, description="YYYY-MM-DD (기본: 오늘)")):
    """상단 요약 — 봇 생존 / 오늘 손익 / 보유 / 체크리스트 집계를 한 번에.

    🔴 **과거 날짜는 스냅샷을 우선한다.** 체크리스트의 일부 항목(물타기 발동
    등)은 로그에만 흔적이 남는데 로그는 10MB에서 로테이션되어 사라진다.
    스냅샷은 그날 로그가 살아있을 때 굳혀둔 값이라 **지금 다시 계산한 것보다
    정확하다.** 오늘 날짜는 항상 실시간으로 계산한다(스냅샷은 마감 후 생긴다).
    """
    d = date or q.today()
    snap = journal.load(d) if d != q.today() else None

    if snap and snap.get("checklist"):
        items = {"ok": True, "data": snap["checklist"]}
        ck_sum = snap.get("checklist_summary")
        source = "snapshot"
    else:
        items = _safe(checklist.build, d)
        ck_sum = checklist.summarize(items["data"]) if items.get("ok") else None
        source = "live"

    return {
        "date": d,
        "source": source,
        "saved_at": (snap or {}).get("saved_at"),
        "has_snapshot": journal.load(d) is not None,
        "bot": _safe(q.bot_status),
        "summary": _safe(q.day_summary, d),
        "holdings": _safe(q.holdings),
        "checklist": items,
        "checklist_summary": ck_sum,
    }


@app.get("/api/journal")
def api_journal():
    """저장된 매매일지 스냅샷 날짜 목록."""
    return _safe(journal.available)


@app.get("/api/trades")
def api_trades(date: str | None = None):
    d = date or q.today()
    return {
        "date": d,
        "trades": _safe(q.trades, d),
        "exit_breakdown": _safe(q.exit_reason_breakdown, d),
    }


@app.get("/api/diagnostics")
def api_diagnostics(date: str | None = None):
    d = date or q.today()
    return {
        "date": d,
        "watchlist": _safe(q.watchlist_summary, d),
        "events": _safe(q.system_events, d),
    }


@app.get("/api/logs")
def api_logs(lines: int = 120, grep: str | None = None, date: str | None = None):
    """로그 tail. `date`가 과거면 로테이션분까지 훑어 그날 줄만 모은다."""
    return _safe(q.log_tail, lines, grep, date)


@app.get("/api/health")
def api_health():
    """DB·로그가 잡히는지. 브라우저 없이 확인할 때 쓴다."""
    bot = _safe(q.bot_status)
    db = _safe(q.day_summary, q.today())
    return {"db_ok": db.get("ok"), "log_ok": bot.get("ok"), "bot": bot, "db": db}


def main() -> None:
    import uvicorn

    if HOST not in ("127.0.0.1", "localhost", "::1"):
        print("=" * 70, file=sys.stderr)
        print(f"⚠️  경고: 루프백이 아닌 주소에 바인딩합니다 -> {HOST}", file=sys.stderr)
        print("    실계좌 현황이 네트워크에 노출됩니다. 의도한 것인지 확인하세요.", file=sys.stderr)
        print("=" * 70, file=sys.stderr)

    print(f"\n  AutoTrader 대시보드 (읽기 전용)  ->  http://{HOST}:{PORT}\n")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
