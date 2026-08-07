# -*- coding: utf-8 -*-
"""장전 일봉 눌림 스캐너 — 🔴 **관측 전용. 매매에 절대 영향이 없다.**

(2026-08-08 신설, Phase 0)

[왜 만들었나]
조건검색 편입 시점에 그냥 샀다면 +5분 **+0.506%**인데, 우리 진입 로직을
통과한 뒤에는 **+0.099%**다(4일 158건 vs 75건). 종목 선별에는 우위가 있는데
'지금인가'를 재는 과정이 그 우위를 1/5로 깎는다.
-> 선별을 **장 시작 전으로 당겨** 두고, 장중엔 확인만 하자는 것이 이 스캐너다.

[왜 조건검색이 아니라 일봉인가]
조건검색식은 **당일 데이터**로 평가된다(3분봉 30이평·20봉내 급등·유동성).
장 시작 전엔 그 데이터가 없어서 08:59 스냅샷이 구조적으로 0종목이다.
반면 **일봉은 전날 종가로 확정**돼 있어 장전에 계산할 수 있다.

[근거]
  눌림 패턴 자체        1,894건  +5일 +2.00%  (기준선 +1.55% — 우위 거의 없음)
  눌림 + 재상승 트리거    549건  +5일 +4.66%  (대조 +2.18%)
  우리 실거래 태그 17건        +0.68% 승률 59% (그 외 -0.36%/41%, 4일 중 3일 우세)
즉 **패턴만으로는 우위가 없고, '돌파선을 넘는 순간'이 있어야 한다.**
이 스캐너가 매일 아침 산출하는 `돌파선`이 바로 그 트리거 기준가다.

[⚠️ 한계 — 결과를 읽을 때 반드시 같이 볼 것]
· +4.66%는 **+5일 보유** 전제다. 우리는 15:10 당일청산이라 지평이 다르다.
  당일에 가장 가까운 숫자는 +1일 **+1.25%**(대조 +0.73%)다.
· 549건의 **중앙값은 +0.19%**, 승률 50%다. 소수의 대박이 평균을 만든다.
· 기간 5구간 중 1개가 마이너스(25년 5~8월 -0.24%).
· 등급(A/B/C)은 **데이터로 고른 값이 아니라 가독성용 초기 가설**이다.
  Phase 0의 목적은 이 등급이 실제로 유효한지 **측정**하는 것이다.

[설계 원칙]
1. **좁게 거르지 않는다.** 필터를 세게 걸면 나중에 "그 밖은 어땠나"를 알 수
   없다. 눌림으로 판정된 것은 등급과 무관하게 **전부 기록**하고, 거르는 것은
   분석 시점에 한다.
2. **판정은 라이브와 같은 함수를 쓴다** — `StrategyManager.daily_pullback_state`.
   복제하면 언젠가 갈라진다(이 코드베이스의 반복 사고 1위).
3. **매매 코드를 건드리지 않는다.** 이 파일은 독립 프로세스이고 주문·DB쓰기가
   없다. 읽는 것은 일봉(ka10081)과 로그와 trades 조회뿐이다.
4. 08:30에 돌아 **08:59 봇 기동 전에 끝난다**. 두 프로세스가 겹치지 않으므로
   토큰·REST 충돌이 원천적으로 없다.

[실행]
    python premarket_scan.py            # 오늘 스캔 + 어제 결과 복기
    python premarket_scan.py --no-send  # 텔레그램 없이(테스트용)
    python premarket_scan.py --date 20260807   # 특정일 기준으로 재현
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys
import traceback
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.auth import get_access_token, send_telegram
from api.kiwoom_rest import KiwoomREST
from config import settings
from utils.logger import logger

OUT_DIR = os.path.join("observations", "premarket")

# ── 유니버스 ────────────────────────────────────────────────────
# 전 종목을 돌면 REST가 수천 콜이 된다. 최근 며칠 조건검색에 걸린 적이 있는
# 종목이면 "HTS 조건식이 관심을 보인 적 있는 모집단"이라 충분히 좁고,
# 우리가 실제로 살 수 있는 종목과 겹친다.
UNIVERSE_DAYS = 5           # 최근 며칠치 편입 로그를 볼지
UNIVERSE_MAX = 220          # REST 상한(0.6초 간격이라 220콜 ≈ 2분 20초)

# ── 등급 경계 (⚠️ 가설이다. Phase 0에서 검증할 대상) ─────────────
# 돌파선까지의 거리 = (돌파선 - 전일종가) / 전일종가
NEAR_PCT = 3.0              # 이 안이면 '오늘 닿을 만하다'
REACH_PCT = 7.0             # 이 밖이면 '오늘은 어렵다'
LIQ_MIN_EOK = 20.0          # 최근 5일 평균 거래대금 하한(억). 참고용 표시

# 편입 로그: "📈 편입 신호: 073240 (금호타이어) - 조건: 돌파자동매매용"
ENTRY_PAT = re.compile(
    r"\[(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\].*?strategy_manager\.py:\d+\] "
    r"📈 편입 신호: (\S{6}) \((.*?)\) - 조건: (.+?)\s*$"
)


# ════════════════════════════════════════════════════════════════
# 1. 유니버스
# ════════════════════════════════════════════════════════════════
def build_universe(today: str) -> dict:
    """최근 UNIVERSE_DAYS 거래일에 조건검색으로 편입된 종목.

    반환 {code: {"name":..., "conds":[...], "days":set(), "last":"YYYY-MM-DD"}}
    로그가 로테이션돼 며칠치가 없을 수 있다 — 그건 정상이고, 그만큼 좁아질 뿐이다.
    """
    seen: dict[str, dict] = {}
    dates: set[str] = set()
    for f in sorted(glob.glob(os.path.join("logs", "autotrader.log*"))):
        try:
            fh = io.open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                m = ENTRY_PAT.search(line)
                if not m:
                    continue
                day, code, name, cond = m.groups()
                if day.replace("-", "") > today:      # --date 재현 시 미래 차단
                    continue
                dates.add(day)
                e = seen.setdefault(code, {"name": name, "conds": set(),
                                           "days": set(), "last": day})
                e["conds"].add(cond.strip())
                e["days"].add(day)
                e["last"] = max(e["last"], day)
                # ⚠️ 08-03 이전 로그의 종목명은 "조건검색"이다 — 키움 실시간
                # 편입 push의 최상위 'name'이 종목명이 아니라 실시간 타입
                # 라벨이었던 버그의 잔재(그날 히스토리 참고). 이름으로 쓰면
                # 리포트가 전부 "조건검색"이 되므로 가짜 이름은 버린다.
                if name and name not in (code, "조건검색"):
                    e["name"] = name

    recent = sorted(dates)[-UNIVERSE_DAYS:]
    uni = {c: e for c, e in seen.items() if e["days"] & set(recent)}
    # 최근에 자주 걸린 종목 우선 — REST 상한에 걸릴 때 무엇을 버릴지의 기준
    ranked = sorted(uni.items(),
                    key=lambda kv: (kv[1]["last"], len(kv[1]["days"])), reverse=True)
    if len(ranked) > UNIVERSE_MAX:
        logger.warning("장전스캔: 유니버스 %d개 -> 상한 %d개로 자름",
                       len(ranked), UNIVERSE_MAX)
        ranked = ranked[:UNIVERSE_MAX]
    for _, e in ranked:
        e["conds"] = sorted(e["conds"])
        e["days"] = sorted(e["days"])
    return dict(ranked), recent


# ════════════════════════════════════════════════════════════════
# 2. 판정 (라이브와 동일한 함수를 쓴다)
# ════════════════════════════════════════════════════════════════
def make_judge(now_dt: datetime):
    """StrategyManager를 __init__ 없이 만들어 판정 메서드만 빌려 쓴다.

    ⚠️ 복제하지 않는 것이 핵심이다. 라이브 태그와 스캐너가 다른 규칙을 쓰면
    "스캐너가 뽑았는데 태그가 안 붙는" 상황이 생기고, 그때 어느 쪽이 맞는지
    알 수 없다.
    """
    import core.strategy_manager as SM

    # 이 상수는 라이브에서 'REST 비용'을 끄기 위한 스위치다. 스캐너는 독립
    # 프로세스라 자기 REST 예산을 스스로 쓰므로, 라이브가 꺼져 있어도 판정은
    # 돌려야 한다. 이 프로세스에만 적용되며 봇에는 영향이 없다.
    SM.DAILY_PULLBACK_TAG_ENABLED = True

    sm = SM.StrategyManager.__new__(SM.StrategyManager)
    sm._daily_bars = {}
    sm._now = lambda: now_dt
    return sm, SM


def grade(dist_pct: float | None, liq_eok: float) -> str:
    """굵은 등급. 연속 점수를 쓰지 않는 이유는 모듈 docstring 참고.

    ⚠️ 이 경계는 **가설**이다. Phase 0의 산출물로 검증한다.
    """
    if dist_pct is None:
        return "C"
    if dist_pct < 0:
        return "돌파됨"                      # 이미 돌파선 위 = 어제 트리거 발생
    if dist_pct <= NEAR_PCT and liq_eok >= LIQ_MIN_EOK:
        return "A"
    if dist_pct <= REACH_PCT:
        return "B"
    return "C"


def scan(rest, uni: dict, now_dt: datetime) -> tuple[list, dict]:
    """유니버스를 돌며 눌림 판정. (후보리스트, 일봉캐시)"""
    sm, SM = make_judge(now_dt)
    today = now_dt.strftime("%Y%m%d")
    out, bars_all, stats = [], {}, Counter()

    for i, (code, meta) in enumerate(uni.items(), 1):
        try:
            bars = rest.get_daily_candles(code, count=60)
        except Exception:
            logger.exception("장전스캔: [%s] 일봉 조회 실패", code)
            stats["조회실패"] += 1
            continue
        if not bars:
            stats["일봉없음"] += 1
            continue
        bars_all[code] = bars
        sm._daily_bars[code] = {"bars": bars, "asof": today}

        st = sm.daily_pullback_state(code)
        if not st:
            stats["눌림아님"] += 1
            continue

        # 완결된 마지막 봉 = 전일(스캔 시점엔 오늘 봉이 아직 없다)
        done = [b for b in bars if b["dt"] < today]
        if not done:
            stats["전일봉없음"] += 1
            continue
        prev = done[-1]
        prev_close = prev["c"]
        line = st["pullback_high"]
        dist = ((line - prev_close) / prev_close * 100) if prev_close else None
        liq = (sum(b["c"] * b["v"] for b in done[-5:]) / min(5, len(done))) / 1e8

        out.append({
            "code": code,
            "name": meta["name"],
            "conds": meta["conds"],
            "last_seen": meta["last"],
            "grade": grade(dist, liq),
            "gap": st["gap"],                     # 폭발일까지 거래일수
            "drops": st.get("drops") or [],       # D+1, D+2... 눌림 깊이
            "spike_dt": st["spike_dt"],
            "spike_vol": st["spike_vol"],
            "breakout_line": line,                # ★ 오늘 넘어야 할 가격
            "prev_close": prev_close,
            "dist_pct": round(dist, 2) if dist is not None else None,
            "liq_eok": round(liq, 1),
        })
        stats["눌림"] += 1

    order = {"돌파됨": 0, "A": 1, "B": 2, "C": 3}
    out.sort(key=lambda r: (order.get(r["grade"], 9),
                            abs(r["dist_pct"] if r["dist_pct"] is not None else 99)))
    logger.info("장전스캔: 유니버스 %d -> %s", len(uni), dict(stats))
    return out, bars_all


# ════════════════════════════════════════════════════════════════
# 3. 어제 후보 복기 — 스캐너가 실제로 맞았는지
# ════════════════════════════════════════════════════════════════
def review(prev_path: str, rest, bars_all: dict, today: str) -> dict | None:
    """직전 스캔의 후보들이 그날 어떻게 됐는지 일봉으로 채점한다.

    이게 없으면 후보 리스트는 그냥 쌓이기만 하고 아무것도 알려주지 않는다.
    오늘 아침이면 어제 일봉이 확정돼 있으므로 추가 비용이 거의 없다
    (대부분 오늘 유니버스에 남아 있어 이미 받아둔 일봉을 재사용한다).
    """
    if not prev_path or not os.path.exists(prev_path):
        return None
    try:
        prev = json.load(io.open(prev_path, encoding="utf-8"))
    except Exception:
        logger.exception("장전스캔: 이전 스캔 파일 읽기 실패 %s", prev_path)
        return None

    target = prev.get("date")
    rows, reused, fetched = [], 0, 0
    for c in prev.get("candidates", []):
        code = c["code"]
        bars = bars_all.get(code)
        if bars:
            reused += 1
        else:
            try:
                bars = rest.get_daily_candles(code, count=20)
                fetched += 1
            except Exception:
                bars = None
        day = next((b for b in (bars or []) if b["dt"] == target), None)
        if not day:
            continue
        line = c["breakout_line"]
        o, h, cl = day["o"], day["h"], day["c"]
        rows.append({
            "code": code, "name": c["name"], "grade": c["grade"],
            "line": line,
            "broke": h >= line,                              # 돌파선을 넘었나
            "high_pct": round((h - o) / o * 100, 2) if o else None,   # 시가 대비 고가
            "close_pct": round((cl - o) / o * 100, 2) if o else None, # 시가 대비 종가
        })

    if not rows:
        return None

    # 우리가 실제로 샀는지 (DB는 읽기만 한다)
    bought = {}
    try:
        from db.connection import get_connection
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT stock_code, profit_rate FROM trades WHERE trade_date = %s",
                (f"{target[:4]}-{target[4:6]}-{target[6:]}",))
            for sc, pr in cur.fetchall():
                bought.setdefault(sc, []).append(float(pr or 0))
    except Exception:
        logger.exception("장전스캔: 복기용 DB 조회 실패(무시하고 계속)")

    for r in rows:
        r["bought"] = len(bought.get(r["code"], []))
        r["realized_pct"] = round(sum(bought.get(r["code"], [])), 2) or None

    by_grade = {}
    for g in ("돌파됨", "A", "B", "C"):
        sub = [r for r in rows if r["grade"] == g]
        if not sub:
            continue
        hs = [r["high_pct"] for r in sub if r["high_pct"] is not None]
        by_grade[g] = {
            "n": len(sub),
            "돌파": sum(1 for r in sub if r["broke"]),
            "평균고가%": round(sum(hs) / len(hs), 2) if hs else None,
            "매수됨": sum(1 for r in sub if r["bought"]),
        }
    logger.info("장전스캔 복기(%s): 재사용 %d / 추가조회 %d", target, reused, fetched)
    return {"date": target, "rows": rows, "by_grade": by_grade}


# ════════════════════════════════════════════════════════════════
# 4. 리포트
# ════════════════════════════════════════════════════════════════
def render(today: str, cands: list, rv: dict | None, uni_n: int, days: list) -> str:
    L = [f"🔭 장전 일봉 눌림 스캔 ({today[:4]}-{today[4:6]}-{today[6:]})",
         "관측 전용 — 매매에 영향 없음", ""]
    if rv and rv.get("by_grade"):
        L.append(f"📊 어제({rv['date']}) 후보 복기")
        for g, s in rv["by_grade"].items():
            L.append(f"  {g}: {s['n']}개 · 돌파 {s['돌파']} · "
                     f"평균고가 {s['평균고가%']}% · 봇매수 {s['매수됨']}")
        L.append("")
    L.append(f"유니버스 {uni_n}개 (최근 {len(days)}거래일 편입) -> 눌림 {len(cands)}개")
    if not cands:
        L.append("")
        L.append("오늘 후보 없음 — 눌림 조건을 만족한 종목이 없다.")
        return "\n".join(L)

    L.append("")
    for g in ("돌파됨", "A", "B", "C"):
        sub = [c for c in cands if c["grade"] == g]
        if not sub:
            continue
        tag = {"돌파됨": "⚡ 이미 돌파선 위(어제 트리거)", "A": "🟢 A (돌파선 근접)",
               "B": "🟡 B", "C": "⚪ C (돌파선 멀거나 얇음)"}[g]
        L.append(f"{tag} — {len(sub)}개")
        for c in sub[:12]:
            dp = f"{c['dist_pct']:+.1f}%" if c["dist_pct"] is not None else "-"
            dr = "/".join(f"{d*100:.0f}" for d in c["drops"]) or "-"
            L.append(f"  {c['name'][:9]}({c['code']}) 돌파선 {c['breakout_line']:,} "
                     f"({dp}) D-{c['gap']} 눌림{dr}% {c['liq_eok']:.0f}억")
        if len(sub) > 12:
            L.append(f"  … 외 {len(sub)-12}개")
        L.append("")
    L.append("※ 돌파선 = 눌림 구간 고가. 이 값을 거래량과 함께 넘을 때가 트리거다.")
    L.append("※ 등급 경계는 아직 검증 전 가설이다(Phase 0 측정 대상).")
    return "\n".join(L)


# ════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-send", action="store_true", help="텔레그램 전송 생략")
    ap.add_argument("--date", default=None, help="기준일(YYYYMMDD) — 재현용")
    args = ap.parse_args()

    now_dt = (datetime.strptime(args.date, "%Y%m%d").replace(hour=8, minute=30)
              if args.date else datetime.now())
    today = now_dt.strftime("%Y%m%d")
    os.makedirs(OUT_DIR, exist_ok=True)

    # ⚠️ 주말 가드 — 스케줄러는 '매일'이라 토·일에도 08:30에 뜬다.
    # 그때 스캔하면 (a) REST 100여 콜을 버리고 (b) 거래가 없는 날짜로 후보
    # 파일이 생겨 다음 거래일 복기가 그 날짜의 일봉을 찾다가 실패한다.
    # (휴장일은 여기서 못 거르지만, 복기가 '일봉이 있는 가장 최근 스캔'을
    #  거슬러 찾으므로 자동으로 건너뛴다 — pick_prev 참고.)
    if now_dt.weekday() >= 5 and not args.date:
        logger.info("장전스캔: %s는 주말(%s) — 스캔 생략",
                    today, "토일"[now_dt.weekday() - 5])
        return 0

    logger.info("=" * 60)
    logger.info("장전 일봉 눌림 스캔 시작 (기준일 %s, IS_MOCK=%s)",
                today, settings.IS_MOCK)

    uni, days = build_universe(today)
    if not uni:
        msg = "🔭 장전 스캔: 유니버스가 비었다(편입 로그 없음) — 스캔 생략"
        logger.warning(msg)
        if not args.no_send:
            send_telegram(msg, target="signal")
        return 0

    rest = KiwoomREST(get_access_token(), is_mock=settings.IS_MOCK)
    cands, bars_all = scan(rest, uni, now_dt)

    # 직전 스캔 복기 — **가장 최근 파일이 아니라 '채점 가능한' 가장 최근 파일**을
    # 쓴다. 휴장일에 만들어진 스캔은 그날 일봉이 없어 채점이 통째로 비는데,
    # 그걸 마지막 파일로 잡으면 **직전 거래일 복기를 영영 못 본다.**
    # 최근 5개까지 거슬러 올라가며 채점되는 것을 찾는다.
    prevs = sorted(p for p in glob.glob(os.path.join(OUT_DIR, "*.json"))
                   if os.path.basename(p)[:8] < today)
    rv = None
    for p in reversed(prevs[-5:]):
        rv = review(p, rest, bars_all, today)
        if rv:
            break
        logger.info("장전스캔: %s는 채점 불가(휴장 추정) — 더 거슬러 올라감",
                    os.path.basename(p)[:8])

    payload = {
        "date": today,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe_size": len(uni),
        "universe_days": days,
        "params": {
            "UNIVERSE_DAYS": UNIVERSE_DAYS, "NEAR_PCT": NEAR_PCT,
            "REACH_PCT": REACH_PCT, "LIQ_MIN_EOK": LIQ_MIN_EOK,
        },
        "candidates": cands,
        "review_of_prev": rv,
    }
    path = os.path.join(OUT_DIR, f"{today}.json")
    json.dump(payload, io.open(path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    logger.info("장전스캔: 후보 %d개 -> %s", len(cands), path)

    report = render(today, cands, rv, len(uni), days)
    print(report)
    if not args.no_send:
        send_telegram(report, target="signal")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        tb = traceback.format_exc()
        logger.exception("장전 스캔 실패")
        try:
            send_telegram(f"🔴 장전 스캔 실패\n{tb[-600:]}", target="signal")
        except Exception:
            pass
        sys.exit(1)
