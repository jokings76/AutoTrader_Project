"""매매일지 스냅샷 저장 — 장 마감 후 1회 실행 (2026-08-11 신설).

사용법:
    python daily_journal.py                 # 오늘 저장
    python daily_journal.py 2026-08-10      # 특정 날짜
    python daily_journal.py --backfill 30   # 최근 30일 중 거래가 있던 날 전부
    python daily_journal.py --list          # 저장된 날짜 목록

⚠️ **봇과 완전히 별도 프로세스다.** 매매 코드를 한 줄도 건드리지 않는다
   (`premarket_scan.py`와 같은 패턴). 봇이 돌든 말든 영향이 없다.

왜 장 마감 후인가
-----------------
로그는 10MB에서 로테이션되어 사라지는데, **물타기 발동 횟수·수동 추가매수
감지는 로그에만 남는다**(DB의 `trades`는 추가매수를 기존 행에 합쳐버린다).
며칠 지나면 그날의 판정을 원리적으로 재현할 수 없으므로, 로그가 살아있는
그날 안에 굳힌다.

작업 스케줄러 등록 (매일 15:40):
    schtasks /Create /TN "AutoTrader_Journal" /SC DAILY /ST 15:40 ^
      /TR "cmd /c cd /d C:\\AutoTrader_Bot\\ProjectRoot && python daily_journal.py"
"""
from __future__ import annotations

import sys

from ui import journal


def _usage() -> None:
    print(__doc__)


def main(argv: list[str]) -> int:
    args = argv[1:]

    if args and args[0] in ("-h", "--help"):
        _usage()
        return 0

    if args and args[0] == "--list":
        dates = journal.available()
        if not dates:
            print("저장된 스냅샷이 없습니다.")
            return 0
        print(f"저장된 스냅샷 {len(dates)}건:")
        for d in dates:
            snap = journal.load(d) or {}
            s = snap.get("summary") or {}
            cs = snap.get("checklist_summary") or {}
            print(
                f"  {d}  청산 {s.get('closed', '?')}건 / {s.get('pnl', '?')}원"
                f"   [정상 {cs.get('ok', '?')} 주의 {cs.get('warn', '?')}"
                f" 이상 {cs.get('bad', '?')} 판정불가 {cs.get('na', '?')}]"
            )
        return 0

    if args and args[0] == "--backfill":
        days = int(args[1]) if len(args) > 1 else 30
        print(f"최근 {days}일 중 거래가 있던 날을 저장합니다...")
        paths = journal.backfill(days)
        for p in paths:
            print("  저장:", p)
        print(f"완료 — {len(paths)}건")
        print("⚠️ 로그가 이미 로테이션된 날은 로그 기반 항목이 '판정 불가'로 굳습니다(정확한 상태입니다).")
        return 0

    date = args[0] if args else None
    if date is None:
        from ui import queries as q
        date = q.today()

    path = journal.save(date)
    snap = journal.load(date) or {}
    s = snap.get("summary") or {}
    cs = snap.get("checklist_summary") or {}
    print(f"저장 완료: {path}")
    print(
        f"  {date}  청산 {s.get('closed')}건 / {s.get('pnl')}원 / 승률 {s.get('win_rate')}%"
        f" / 평균보유 {s.get('avg_hold_min')}분"
    )
    print(
        f"  체크리스트 — 정상 {cs.get('ok')} · 주의 {cs.get('warn')}"
        f" · 이상 {cs.get('bad')} · 판정불가 {cs.get('na')}"
    )
    bad = [i for i in (snap.get("checklist") or []) if i.get("status") == "bad"]
    for i in bad:
        print(f"  🔴 [{i['rank']}] {i['title']} — {i['actual']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
