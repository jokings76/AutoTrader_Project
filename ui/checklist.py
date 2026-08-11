"""08-12 장중 관찰 체크리스트 — CLAUDE.md 「🔬 장중에 반드시 볼 것」 0~10을 자동 판정.

지금까지 이 표는 **사람이 로그를 grep해서** 채웠다. 08-12엔 미검증 변경이 5건 +
HTS 조건식 변경까지 겹쳐 도는 날이라, 그 표를 화면으로 만드는 것이 이 대시보드의
존재 이유다.

🔴 설계 원칙 — 모르면 '정상'이라고 말하지 않는다
------------------------------------------------
각 항목은 `ok / warn / bad / na` 중 하나를 돌려준다. **`na`(판정 불가)를 성실히
쓰는 것이 이 파일의 핵심이다.** 데이터가 없는데 초록불을 켜면 대시보드가
"괜찮다"고 거짓말을 하고, 그건 표가 없는 것보다 나쁘다.
(이 프로젝트가 반복해서 당한 부류 — 스텁이 실물과 달라 감사가 '통과'로
 거짓말한 사고가 08-09·08-10에 연달아 있었다.)

🔴 소스 선택 근거 (2026-08-11 실측)
-----------------------------------
**추가매수는 `trades` 행을 갱신할 뿐 `entry_reason`을 덮지 않는다.**
실측: 뉴엔AI(463020)가 17주 매수 -> rescue-add 17주 -> 총 34주가 됐는데
`entry_reason`엔 최초 진입 문구만 있고 `entry_note`("손절 대신 추가매수")는
없다. 따라서 **물타기 발동은 `entry_reason`으로 셀 수 없다.**
-> 1차 소스는 **로그**(`strategy_manager.py:6638`이 `💧 물타기`를 반드시 남긴다),
   보조로 `system_events`의 `BUY_ADD`를 쓴다.
"""
from __future__ import annotations

import re

from . import queries as q

# ---------------------------------------------------------------------------
# 기대값 — CLAUDE.md 「기대값」 블록과 같이 움직여야 한다.
#
# ⚠️ 여기에 값을 박아두면 상수를 바꿨을 때 조용히 어긋난다. 그래서 **판정에
#    쓰는 값만** 최소한으로 두고, 나머지는 문구로만 안내한다. 상수를 직접
#    import하지 않는 이유는 이 모듈이 core/를 건드리면 안 되기 때문이다
#    (queries.py 최상단 주석 참고) — 대신 어긋나면 감사가 잡도록 CLAUDE.md에
#    같이 적어 둔다.
# ---------------------------------------------------------------------------
STOP_FLOOR_PCT = -4.5      # 손절 클램프 하한
STOP_CEIL_PCT = -5.5       # 손절 클램프 상한(더 깊은 쪽)
STOP_TOLERANCE = 0.15      # 표시 반올림 여유
TP_CAP_NET_PCT = 6.0       # 익절캡(순)
FEE_PCT = 0.23             # 왕복수수료
EARLY_SLOT_CAP = 4         # 09:00~09:05 동시 점유 상한
AVG_DOWN_BASELINE_RATE = 38.0   # 옛 조건식 기준 -3% 도달률(%)

_LOG_PATTERNS = {
    "avg_down": "💧 물타기",
    "rescue_add": "🛟 손절 대신 추가매수",
    "manual_add": "수동 추가매수 감지",
    "confirm_only": "확인 전용",
    "ghost": "유령",
}


def _item(rank, title, expected, actual, status, detail="", source=""):
    return {
        "rank": rank,
        "title": title,
        "expected": expected,
        "actual": actual,
        "status": status,          # ok | warn | bad | na
        "detail": detail,
        "source": source,
    }


def build(date: str) -> list[dict]:
    """체크리스트 0~10을 계산해 돌려준다."""
    tr = q.trades(date)
    closed = [t for t in tr if t["status"] == "closed"]
    wl = q.watchlist_summary(date)

    # 🔴 `None`이면 **그 날짜의 로그가 없다**(로테이션으로 소실). 0으로 뭉개면
    #    "물타기 0건"이라는 거짓 초록불이 켜진다 — 반드시 `na`로 흘려보낼 것.
    logs = q.log_count(_LOG_PATTERNS, date)
    has_log = logs is not None
    LG = logs or {k: 0 for k in _LOG_PATTERNS}
    _no_log = "그날 로그 없음(로테이션으로 소실) — 스냅샷이 있으면 그쪽을 볼 것"

    items: list[dict] = []

    # --- 0. 새 조건식이 봇 구독에 붙었는가 -------------------------------
    # CLAUDE.md 순위 0의 본질은 "돌파전 편입 종목의 등락률"이지만, 편입 로그에
    # 등락률이 남지 않아 직접 계산이 불가능하다. 대신 **더 직접적인 검사**가
    # 가능하다 — 조건식 이름 자체가 바뀌었는지. 옛 이름이 보이면 그게 곧
    # "새 조건식이 안 붙었다"는 증거다(08-11 13:17 한켐 +9.39% 사례의 원인).
    names = [c["cond_name"] for c in wl["by_condition"]]
    joined = " ".join(names)
    has_old = "돌파자동매매용" in joined
    has_new = "돌파전" in joined
    if not names:
        st, actual = "na", "편입 기록 없음"
    elif has_old:
        st, actual = "bad", f"옛 이름 잔존: {[n for n in names if '돌파자동매매용' in n]}"
    elif has_new:
        st, actual = "ok", f"돌파전 편입 확인 ({', '.join(names)})"
    else:
        st, actual = "warn", f"돌파전 편입 아직 없음 ({', '.join(names) or '-'})"
    items.append(_item(
        0, "새 조건식(돌파전/돌파후) 부착",
        "cond_name에 '돌파전' 등장 · '돌파자동매매용' 소멸", actual, st,
        "옛 이름이 보이면 HTS 조건식이 봇 구독에 안 붙은 것이다. "
        "⚠️ 편입 시 등락률은 로그에 안 남아 8% 초과 여부는 직접 셀 수 없다 — "
        "이름 확인이 가장 직접적인 대체 검사다.",
        "watch_list_log.cond_name",
    ))

    # --- 1. -3% 도달률 --------------------------------------------------
    # 물타기가 정확히 원가 -3%에서 조건 없이 발동하므로, **물타기 발동률이
    # 곧 -3% 도달률**이다. 기준선은 옛 조건식 5일 실측 38%.
    n_buys = len(tr)
    n_avgdown = LG["avg_down"]
    if not has_log:
        st, actual = "na", _no_log
    elif n_buys == 0:
        st, actual = "na", "매수 없음"
    else:
        rate = 100.0 * n_avgdown / n_buys
        actual = f"{rate:.0f}%  ({n_avgdown}/{n_buys}건)"
        if rate < AVG_DOWN_BASELINE_RATE - 5:
            st = "ok"
        elif rate <= AVG_DOWN_BASELINE_RATE + 5:
            st = "warn"
        else:
            st = "bad"
    items.append(_item(
        1, "-3% 도달률", f"옛 조건식 기준선 {AVG_DOWN_BASELINE_RATE:.0f}%보다 낮아야 함",
        actual, st,
        "이게 낮아져야 '돌파전이 고점 매수를 줄였다'가 증명된다. "
        "그대로면 조건식 개편의 효과가 없는 것.",
        "로그 '💧 물타기' ÷ trades 매수 건수",
    ))

    # --- 2. 물타기 발동 건수와 결과 --------------------------------------
    # ⚠️ 물타기는 기존 포지션에 합쳐지므로 '물타기한 종목의 결과'를 DB만으로
    #    분리할 수 없다. 건수는 로그에서, 결과는 사람이 판단하도록 남긴다.
    if not has_log:
        st, actual = "na", _no_log
        detail = "로그가 1차 소스다 — 물타기는 기존 행에 합쳐져 DB로는 셀 수 없다."
    elif n_avgdown == 0:
        st, actual = "ok", "0건"
        detail = "아직 발동 없음. 발동하면 회복 여부를 반드시 볼 것."
    else:
        st = "warn" if n_avgdown < 5 else "bad"
        actual = f"{n_avgdown}건"
        detail = ("하루 5건 이상 + 대부분 손절이면 `AVG_DOWN_ENABLED = False` 검토. "
                  "⚠️ 물타기는 기존 행에 합쳐져 결과 분리가 DB만으론 안 된다 — "
                  "아래 '시스템 이벤트'의 BUY_ADD와 청산 사유를 같이 볼 것.")
    items.append(_item(
        2, "[물타기] 발동 건수", "발동 후 회복하는가 (하루 5건 미만)", actual, st,
        detail + (f"  · 손절대신추가매수(별개) {LG['rescue_add']}건" if has_log else ""),
        "로그 '💧 물타기'",
    ))

    # --- 3. 손절선 분포 --------------------------------------------------
    stops = []
    for t in closed:
        m = re.search(r"손절선\s*(-?\d+\.?\d*)%", t.get("exit_reason") or "")
        if m:
            stops.append(float(m.group(1)))
    if not stops:
        st, actual = "na", "손절 없음"
        detail = "손절이 나오면 종목별로 갈리는지 볼 것."
    else:
        lo, hi = max(stops), min(stops)   # lo = 얕은 쪽
        out = [s for s in stops
               if s > STOP_FLOOR_PCT + STOP_TOLERANCE or s < STOP_CEIL_PCT - STOP_TOLERANCE]
        actual = f"{len(stops)}건 · {hi:.2f}% ~ {lo:.2f}%"
        if out:
            st = "bad"
            detail = (f"🔴 범위 밖 {len(out)}건: {out[:5]} — "
                      "손절 상수가 안 먹었거나 옛 프로세스가 도는 것.")
        elif len(set(round(s, 2) for s in stops)) == 1:
            st = "ok"
            detail = ("전부 같은 값 = 하한에 붙은 것. **정상 범위다** "
                      "(실측 74%가 -4.5%에 붙는다).")
        else:
            st = "ok"
            detail = "종목별 차등이 살아있다."
    items.append(_item(
        3, "손절선 범위", f"{STOP_FLOOR_PCT}% ~ {STOP_CEIL_PCT}%", actual, st,
        detail, "trades.exit_reason 정규식",
    ))

    # --- 4. 돌파후 단독 매수 (0건이 정상) --------------------------------
    confirm_buys = [
        t for t in tr
        if "돌파후" in (t.get("entry_reason") or "")
        and "돌파전" not in (t.get("entry_reason") or "")
        and "주도주상위" not in (t.get("entry_reason") or "")
        and "눌림목자동" not in (t.get("entry_reason") or "")
    ]
    items.append(_item(
        4, "돌파후 단독 매수", "0건 (확인 전용)",
        f"{len(confirm_buys)}건" + (f" — {[t['stock_name'] for t in confirm_buys]}" if confirm_buys else ""),
        "ok" if not confirm_buys else "bad",
        "1건이라도 있으면 `_confirm_only_reject` 배선을 확인할 것. "
        "🔴 resolve_strategy가 미분류를 '1A'로 폴백하므로 source_flags에서 "
        "빼는 것만으론 안 막힌다.",
        "trades.entry_reason",
    ))

    # --- 5. 수동매도 vs 미체결 구분 --------------------------------------
    manual_sell = sum(1 for t in closed if "수동 매도" in (t.get("exit_reason") or ""))
    unfilled = sum(1 for t in closed if "미체결" in (t.get("exit_reason") or ""))
    if manual_sell == 0 and unfilled == 0:
        st, actual = "na", "해당 없음"
    elif unfilled > 0 and manual_sell == 0:
        st, actual = "warn", f"수동매도 0 / 미체결 {unfilled}"
    else:
        st, actual = "ok", f"수동매도 {manual_sell} / 미체결 {unfilled}"
    items.append(_item(
        5, "수동매도 vs 미체결 구분", "사용자가 HTS에서 판 건은 '수동 매도'로",
        actual, st,
        "전부 '미체결'로 찍히면 `seen_on_server` 표식이 안 붙는 것 "
        "(08-11에 6건이 전부 오기록됐다).",
        "trades.exit_reason",
    ))

    # --- 6. 수동 추가매수 감지 -------------------------------------------
    items.append(_item(
        6, "수동 추가매수 감지", "직접 추가매수하면 평단·수량이 합산된다",
        f"{LG['manual_add']}건" if has_log else _no_log,
        "ok" if has_log else "na",
        "직접 추가매수하셨는데 안 뜨면 15초 잔고 대조가 2회 연속 관측을 "
        "못 채운 것. 안 하셨으면 0건이 정상이다.",
        "로그 '수동 추가매수 감지'",
    ))

    # --- 7. 익절캡 발동 수익률 -------------------------------------------
    # profit_rate는 **가격 기준(gross)**이다 (혜인 실측: gross 2.90 vs 순 2.67,
    # 차이 0.23 = 왕복수수료). 캡은 순 6%이므로 가격 기준 약 6.23%.
    tp = [t for t in closed if "익절" in (t.get("exit_reason") or "")]
    if not tp:
        st, actual, detail = "na", "익절캡 발동 없음", "발동하면 순 +6% 부근인지 볼 것."
    else:
        rates = [t["profit_rate"] for t in tp if t["profit_rate"] is not None]
        avg = sum(rates) / len(rates) if rates else 0
        net = avg - FEE_PCT
        actual = f"{len(tp)}건 · 평균 순 {net:+.2f}% (가격 {avg:+.2f}%)"
        st = "ok" if abs(net - TP_CAP_NET_PCT) <= 1.0 else "bad"
        detail = ("순 +4% 부근이면 캡 상수가 안 먹은 것(08-10 상향 전 값)."
                  if st == "bad" else "캡이 정상 동작 중.")
    items.append(_item(
        7, "익절캡 발동 수익률", f"순 +{TP_CAP_NET_PCT:.0f}% 부근", actual, st,
        detail, "trades.profit_rate − 수수료",
    ))

    # --- 8. 꺼져 있어야 할 규칙 ------------------------------------------
    off_hits = {
        "정체 정리": sum(1 for t in closed if "정체" in (t.get("exit_reason") or "")),
        "시간정리": sum(1 for t in closed if "시간정리" in (t.get("exit_reason") or "")),
        "우선순위 교체": sum(1 for t in closed if "우선순위" in (t.get("exit_reason") or "")),
    }
    total_off = sum(off_hits.values())
    items.append(_item(
        8, "꺼진 규칙이 안 도는가", "전부 0건",
        ", ".join(f"{k} {v}" for k, v in off_hits.items()),
        "ok" if total_off == 0 else "bad",
        "1건이라도 있으면 `STAGNANT_EXIT_ENABLED` / "
        "`PHASE1A_PRIORITY_MAX_PER_DAY` 상수를 확인할 것.",
        "trades.exit_reason",
    ))

    # --- 9. 개장초반 슬롯 캡 ---------------------------------------------
    # ⚠️ 캡은 '동시 점유'를 막지 '거래 횟수'를 막지 않는다 — 빠른 청산으로
    #    자리가 비면 다시 산다. 그래서 **매수 건수가 4를 넘는 것 자체는 정상**일
    #    수 있다. 동시 점유를 재려면 청산 시각까지 봐야 하므로, 여기서는
    #    건수를 보여주되 판정은 느슨하게 한다.
    early = [t for t in tr if t.get("buy_time") and "T09:0" in t["buy_time"]
             and t["buy_time"][11:16] < "09:05"]
    n_early = len(early)
    if n_early == 0:
        st = "na" if not tr else "ok"
    elif n_early <= EARLY_SLOT_CAP:
        st = "ok"
    else:
        st = "warn"
    items.append(_item(
        9, "09:00~09:05 매수", f"동시 점유 최대 {EARLY_SLOT_CAP}",
        f"{n_early}건", st,
        "⚠️ 캡은 **동시 점유**를 막지 거래 횟수를 막지 않는다 — 빨리 팔려서 "
        f"자리가 나면 다시 산다. {EARLY_SLOT_CAP}건 초과가 곧 위반은 아니다. "
        "08-06처럼 개장 18분에 19건 수준이면 그때 캡을 의심할 것.",
        "trades.buy_time",
    ))

    # --- 10. 눌림목 매수 (슬롯 0) ----------------------------------------
    pb = [t for t in tr if "눌림" in (t.get("sub_strategy") or "")]
    items.append(_item(
        10, "눌림목 매수", "0건 (슬롯 0 = 매매 중단)",
        f"{len(pb)}건" + (f" — {[t['stock_name'] for t in pb]}" if pb else ""),
        "ok" if not pb else "bad",
        "1건이라도 있으면 `PULLBACK_MAX_SLOTS`를 확인할 것.",
        "trades.sub_strategy",
    ))

    return items


def summarize(items: list[dict]) -> dict:
    """상단 배지용 집계."""
    c = {"ok": 0, "warn": 0, "bad": 0, "na": 0}
    for it in items:
        c[it["status"]] = c.get(it["status"], 0) + 1
    return c
