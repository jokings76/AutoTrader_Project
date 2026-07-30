"""프로그램 유입 종목의 사후 수익률 검증 (2026-07-31 신규).

무엇을 답하는 스크립트인가
────────────────────────
"프로그램 매수가 꾸준히 들어온 종목은 실제로 수익률이 좋았나?"
logs/program_flow/YYYY-MM-DD.csv(ProgramFlowTracker가 남긴 분 단위 기록)를
읽어, 각 종목이 '꾸준한 유입' 판정을 처음 받은 시각을 진입 시점으로 보고
그 이후 N분 뒤의 가격 변화를 측정한다.

핵심 설계 — 결론이 유의미하려면
──────────────────────────
1) **대조군을 반드시 같이 본다.** 프로그램 유입 종목만 보면 "그날 시장이
   좋았을 뿐"인지 알 수 없다. 같은 CSV에 기록됐지만 '꾸준' 판정을 못 받은
   종목을 대조군으로 놓고, 두 집단의 forward return 차이를 본다.
   차이가 없으면 이 신호는 쓸모없는 것이고, 그걸 아는 게 이 스크립트의 목적이다.
2) **여러 지표축을 따로 채점한다.** cum_net(규모) / positive_minutes(꾸준함) /
   max_streak(끊김없음) 중 어느 것이 예측력이 있는지 미리 정하지 않고
   각각 상·하위로 갈라 비교한다. 하나만 검증하면 '내가 고른 정의가 맞았다'는
   확증편향에 빠진다.
3) **여러 홀딩 구간을 같이 본다(5/10/20/30분).** 한 구간에서만 좋으면
   우연일 가능성이 크고, 구간에 따라 단조롭게 변하면 신호가 진짜일 가능성이 높다
   (이 프로젝트가 1B 반등확증·동적 익절캡을 채택할 때 쓴 것과 같은 기준).
4) 표본 수를 항상 같이 출력한다 — 이 프로젝트의 하루 표본은 한 자릿수~수십
   건이라, n을 안 보고 평균만 보면 오독하게 된다(07-29 1B 평균이 오염행 2건
   때문에 -3.26%로 보였던 전례).

사용법
    python analyze_program_flow.py                  # 오늘자
    python analyze_program_flow.py 2026-08-01       # 특정일
    python analyze_program_flow.py 2026-08-01 2026-08-05   # 기간 합산
"""
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
FLOW_DIR = os.path.join(BASE, "logs", "program_flow")
HOLD_MINUTES = [5, 10, 20, 30]


def load_rows(dates: list[str]) -> list[dict]:
    rows = []
    for d in dates:
        path = os.path.join(FLOW_DIR, f"{d}.csv")
        if not os.path.exists(path):
            print(f"  (없음) {path}")
            continue
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                r["_dt"] = datetime.strptime(f"{r['date']} {r['minute']}", "%Y-%m-%d %H:%M")
                for k in ("net", "cum_net_10m"):
                    r[k] = float(r[k] or 0)
                for k in ("positive_minutes_10m", "max_streak_10m", "sustained"):
                    r[k] = int(r[k] or 0)
                rows.append(r)
    return rows


def price_series_from_flow(rows: list[dict]) -> dict:
    """CSV에는 가격이 없으므로 분봉 DB/REST 대신 trades 기록으로 대체 검증할 수
    있게 종목·분 인덱스만 만들어 둔다. 가격이 필요하면 아래 attach_prices 사용."""
    idx = defaultdict(dict)
    for r in rows:
        idx[r["stock_code"]][r["_dt"]] = r
    return idx


def attach_prices(codes: list[str], dates: list[str]) -> dict:
    """분봉을 REST로 받아 (code, datetime) -> close 매핑 생성.

    **장중에는 절대 실행하지 말 것** — 이 계정은 429가 이미 포화 상태다
    (07-30 기준 ka10080만 2,223건). 장 마감 후 분석 용도로만 쓴다."""
    try:
        from api.auth import get_access_token
        from api.kiwoom_rest import KiwoomREST
        from config import settings
    except Exception as e:
        print(f"  가격 조회 모듈 로드 실패: {e}")
        return {}
    token = get_access_token()
    if not token:
        print("  토큰 발급 실패 — 가격 없이 진행")
        return {}
    rest = KiwoomREST(token, is_mock=getattr(settings, "IS_MOCK", True))
    out = {}
    for i, code in enumerate(codes, 1):
        try:
            candles = rest.get_minute_candles(code, interval=1, count=400)
        except Exception as e:
            print(f"  [{code}] 분봉 실패: {e}")
            continue
        for c in candles or []:
            ts = str(c.get("time_str") or "")
            if len(ts) < 12:
                continue
            try:
                dt = datetime.strptime(ts[:12], "%Y%m%d%H%M")
            except ValueError:
                continue
            if f"{dt:%Y-%m-%d}" in dates:
                out[(code, dt)] = float(c.get("close") or 0)
        print(f"  가격 수집 {i}/{len(codes)} {code}", end="\r")
    print()
    return out


def forward_return(prices: dict, code: str, t0: datetime, minutes: int):
    from datetime import timedelta
    p0 = prices.get((code, t0))
    p1 = prices.get((code, t0 + timedelta(minutes=minutes)))
    if not p0 or not p1:
        return None
    return (p1 - p0) / p0 * 100


def summarize(label: str, samples: list[float]):
    if not samples:
        return f"  {label:28s} n=0"
    n = len(samples)
    avg = sum(samples) / n
    wins = sum(1 for s in samples if s > 0)
    return (f"  {label:28s} n={n:4d}  평균 {avg:+6.3f}%  "
            f"승률 {wins/n*100:5.1f}%")


def main():
    args = sys.argv[1:]
    if not args:
        dates = [f"{datetime.now():%Y-%m-%d}"]
    elif len(args) == 1:
        dates = [args[0]]
    else:
        from datetime import timedelta
        d0 = datetime.strptime(args[0], "%Y-%m-%d")
        d1 = datetime.strptime(args[1], "%Y-%m-%d")
        dates = [f"{d0 + timedelta(days=i):%Y-%m-%d}"
                 for i in range((d1 - d0).days + 1)]

    print(f"대상 날짜: {dates}")
    rows = load_rows(dates)
    if not rows:
        print("기록 없음 — 프로그램 유입 소스가 아직 연결되지 않았거나 그날 데이터가 없습니다.")
        return 1
    print(f"총 {len(rows)}행, 종목 {len(set(r['stock_code'] for r in rows))}개\n")

    # 종목별 '꾸준 판정 최초 시각'(진입 시점 후보)
    first_sustained = {}
    all_codes = set()
    for r in sorted(rows, key=lambda x: x["_dt"]):
        all_codes.add(r["stock_code"])
        if r["sustained"] and r["stock_code"] not in first_sustained:
            first_sustained[r["stock_code"]] = r
    control_codes = all_codes - set(first_sustained)
    print(f"꾸준 유입 판정 종목: {len(first_sustained)}개")
    print(f"대조군(판정 못 받은 종목): {len(control_codes)}개\n")

    if not first_sustained:
        print("꾸준 판정 종목이 없어 비교 불가. 임계값(SUSTAIN_*) 완화를 검토하세요.")
        return 0

    prices = attach_prices(sorted(all_codes), dates)
    if not prices:
        print("가격 데이터가 없어 수익률 비교를 건너뜁니다 "
              "(장 마감 후 재실행하면 분봉을 받아옵니다).")
        return 0

    # 대조군 진입시점: 그 종목의 첫 기록 시각
    first_any = {}
    for r in sorted(rows, key=lambda x: x["_dt"]):
        first_any.setdefault(r["stock_code"], r)

    print("=" * 70)
    print("[A] 꾸준 유입 종목 vs 대조군 — forward return")
    print("=" * 70)
    for hold in HOLD_MINUTES:
        sus = [x for x in (forward_return(prices, c, r["_dt"], hold)
                           for c, r in first_sustained.items()) if x is not None]
        ctl = [x for x in (forward_return(prices, c, first_any[c]["_dt"], hold)
                           for c in control_codes) if x is not None]
        print(f"\n  ── {hold}분 보유 ──")
        print(summarize("꾸준 유입", sus))
        print(summarize("대조군", ctl))
        if sus and ctl:
            diff = sum(sus) / len(sus) - sum(ctl) / len(ctl)
            print(f"  {'차이(유입-대조)':28s} {diff:+6.3f}%p")

    print("\n" + "=" * 70)
    print("[B] 지표축별 상·하위 비교 (어느 정의가 예측력이 있는가)")
    print("=" * 70)
    for axis in ("cum_net_10m", "positive_minutes_10m", "max_streak_10m"):
        entries = [(c, r) for c, r in first_sustained.items()]
        if len(entries) < 4:
            print(f"\n  {axis}: 표본 부족(n={len(entries)}) — 건너뜀")
            continue
        entries.sort(key=lambda x: x[1][axis], reverse=True)
        half = len(entries) // 2
        print(f"\n  ── {axis} ──")
        for hold in (10, 30):
            hi = [x for x in (forward_return(prices, c, r["_dt"], hold)
                              for c, r in entries[:half]) if x is not None]
            lo = [x for x in (forward_return(prices, c, r["_dt"], hold)
                              for c, r in entries[half:]) if x is not None]
            print(f"    [{hold}분] " + summarize("상위 절반", hi).strip())
            print(f"    [{hold}분] " + summarize("하위 절반", lo).strip())

    print("\n" + "=" * 70)
    print("해석 가이드")
    print("  - [A]에서 '차이'가 여러 홀딩 구간에 걸쳐 일관되게 (+)여야 신호로 인정.")
    print("    한 구간만 좋으면 우연일 가능성이 큽니다.")
    print("  - [B]에서 상위/하위가 갈리지 않으면 그 지표축은 예측력이 없는 것.")
    print("  - n이 20 미만이면 어떤 결론도 내리지 마세요(며칠 더 모을 것).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
