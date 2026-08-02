"""
일일 백테스트 — 그날 실제 조건검색/테마 신호를 받은 종목(watch_list_log + trades)을
대상으로, 라이브와 동일한 진입 스코어링 함수(core/strategy/scoring.py, vwap_strategy.py)와
청산 정책 상수(core/strategy_manager.py)를 그대로 재사용해 분봉 기준으로 매매를 재현한다.
(2026-07-27: 1S/1N/Phase2/Phase3 삭제된 신규 전략 구조에 맞춰 갱신 — 1A/Pullback만 재현)
(2026-07-31: 1A가 체결강도 단독(틱 필요) 방식으로 전면 교체되며 재현 대상에서
제외 — 아래 "Pullback만 재현" 참고, 1L도 이날 주석처리되어 이제 라이브에서도
안 돎)

⚠️ (2026-08-02) **이제 라이브 진입을 재현하지 못한다.** Pullback까지 틱 구동
(체결강도 FID 228 100 이상 3초 연속 + 대량체결 버스트)으로 바뀌면서, 분봉만
가진 이 백테스트가 재현할 수 있는 진입 로직이 하나도 남지 않았다. 그런데도
숫자는 그럴듯하게 나오므로(눌림목 점수 경로가 그대로 살아있음) **진입 성과로
읽으면 안 된다** — 리포트 상단에 경고를 넣어 두었다.
남겨두는 이유는 청산 정책(손절/익절캡/시간정리) 비교엔 여전히 쓸 수 있기
때문이다. 진입까지 제대로 재현하려면 `cache/tick_history/`에 쌓이는 틱
데이터를 TradeFlowTracker.add_tick()에 먹이는 별도 하니스가 필요하다.

한계 (심플하게 유지하기 위한 의도적 단순화):
  - **1A/1B/1L 전부 제외, Pullback만 재현**(2026-07-31 갱신). 1A가
    evaluate_1a_leading_strength(체결강도 100 이상 1분 유지)로 바뀌면서
    1B(체결강도 FSM)/1L(주도주 실시간강도 지속)과 같은 이유(틱 단위 체결강도
    데이터 필요, 1분봉만으로 재현 불가)로 제외 대상에 합류했다. 1L은 이날
    라이브에서도 주석처리되어(1A와 설계 중복 판단) 실제로 안 도는 상태.
  - 체결강도(current_strength)는 분봉만으로 알 수 없어 중립값 100.0 고정
    (라이브에서 phase1b 없을 때 쓰는 fallback과 동일). 지수 방어 CAUTION
    가산점도 같은 이유로 재현하지 않음(단, 그 가산점을 쓰던 옛 1A 경로 자체가
    이제 안 돎).
  - 종목별 독립 시뮬레이션 (슬롯/동시보유 한도 미반영) — 진입/청산 규칙 자체의
    유효성을 보는 게 목적이라 자금 배분 제약은 넣지 않음.

실행: 이 모듈은 main.py의 태스크에서 run_daily_backtest(rest_api)로 호출됨.
"""
import re
from datetime import date, time as dtime
from collections import defaultdict

from utils.logger import logger
from api.auth import send_telegram
from db.connection import get_cursor
from core.strategy.indicators import is_volume_increasing_streak
from core.strategy.scoring import (
    ScoreConfig,
    score_phase1,
    score_pullback,
)
from core.strategy.vwap_strategy import VWAPStrategy, calc_vwap
from core.strategy_manager import (
    STOP_LOSS_RATE,
    TAKE_PROFIT_CAP,
    TAKE_PROFIT_CAP_PULLBACK,
    TAKE_PROFIT_CAP_EARLY,
    EARLY_WINDOW_END,
    HOLDING_TIMEOUT,
    ROUND_TRIP_COST,
    VOLUME_LOOKBACK,
    PHASE1A_SCORE_NORMAL,
    PHASE1A_SCORE_TIGHT,
    PULLBACK_START,
    PULLBACK_END,
    MAX_BUYS_PER_STOCK,
    REBUY_COOLDOWN,
)

NEUTRAL_STRENGTH = 100.0  # 틱데이터 없어 체결강도는 중립값 고정
FORCE_CLOSE_HHMM = "1510"  # 라이브 FORCE_CLOSE_TIME과 동일 (2026-08-01: 1515 -> 1510)
FETCH_COUNT = 450  # 09:00~15:20 + 60MA 여유
GROUP_A_START_HHMM = "0900"
# (2026-08-01) Pullback 시간창 09:20~15:10 확대를 라이브 상수에서 직접 가져온다
# — 예전엔 "1030"을 문자열로 박아둬서 라이브를 바꿔도 백테스트가 안 따라왔다.
PULLBACK_START_HHMM = PULLBACK_START.strftime("%H%M")
PULLBACK_END_HHMM = PULLBACK_END.strftime("%H%M")
PHASE1A_TIGHTEN_HHMM = "1030"
PHASE1A_END_HHMM = "1450"
EARLY_WINDOW_END_HHMM = EARLY_WINDOW_END.strftime("%H%M")  # 개장초반 익절 1.5% 경계
# 라우팅: cond_name에 이 이름이 있으면 Pullback 전용, 없으면 1A 전용(상호배타).
PULLBACK_ONLY_SOURCE = "눌림목자동"


def _hhmm_to_time(hhmm: str):
    """'0925' -> datetime.time(9,25). 라이브 라우팅 함수에 넘기기 위한 변환."""
    from datetime import time as _t
    return _t(int(hhmm[:2]), int(hhmm[2:]))

EXIT_CATEGORY_ORDER = ["손절", "익절", "시간정리", "강제청산"]

# 체결강도/OBV는 틱데이터가 있어야 계산되는데 백테스트는 분봉만 갖고 있어서
# 항상 고정값(NEUTRAL_STRENGTH=100, obv_momentum 기본값 0.0)을 씀. 문제는
# _f_strength(100, ...)이 "중립"이 아니라 "0점"으로 계산된다는 것(공식상
# 100~120을 0~1로 매핑해서 100은 곧 하한) — 그래서 이 두 요소를 가중치 넣은
# 채로 두면 실제로는 매번 0점을 깔고 시작하는 건데 만점 기준(threshold)엔
# 그대로 반영돼서 다른 요소가 훨씬 더 잘 나와야만 통과하는 부당한 페널티가
# 됨(2026-07-29 실측: 세아메카닉스 하루 24개 후보 중 여러 건이 0.3~0.4점
# 차이로 전부 탈락, 강도만 뻤으면 다 통과할 점수였음). 그래서 백테스트
# 전용 cfg에서는 이 두 요소의 가중치를 0으로 둬서 점수/만점 계산에서
# 아예 빼버림(측정 불가한 걸 "중립"이 아니라 "항상 최저"로 반영하던 버그 수정).
PHASE1A_CFG = ScoreConfig(w_strength=0.0)

# Pullback 전용 점수 cfg — core/strategy_manager.py의 self.pullback_score_cfg와
# "가중치 배분 철학"은 같게 유지하되(MA/양봉 점수 제외), 체결강도/OBV는 위와
# 같은 이유로 백테스트에서는 0으로 둠(라이브에선 phase1b/실시간 캔들로 실제
# 값이 있어서 self.pullback_score_cfg는 w_strength=3.0/w_obv=2.0 그대로 유지).
PULLBACK_CFG = ScoreConfig(w_volume=4.0, w_strength=0.0, w_obv=0.0, threshold_ratio=0.5)


def _hhmm(candle: dict) -> str:
    return candle["time_str"][8:12]


def _volume_ratio(sub: list) -> float:
    if len(sub) < 1 + VOLUME_LOOKBACK:
        return 0.0
    cur_vol = sub[0]["volume"]
    prev = sub[1 : 1 + VOLUME_LOOKBACK]
    avg = sum(c["volume"] for c in prev) / len(prev)
    return cur_vol / avg if avg > 0 else 0.0


def _get_today_universe() -> list:
    """오늘 watch_list_log + trades에 기록된 종목(그날 실제 신호/매매 종목) 유니버스.

    각 종목이 어떤 조건검색식(주도주상위/눌림목자동/돌파자동매매용)으로
    편입됐는지(cond_name)도 함께 가져온다(2026-07-31) — 라이브의
    on_condition_hit/_evaluate_1a_pullback_entry가 cond_name에 따라 다른
    경로(skip_setup_check, 09:20 지연게이트)를 타는데, 기존엔 이 출처 정보가
    백테스트에 전혀 없어서 재현이 불가능했다.

    watch_list_log.cond_name이 우선(매수 안 된 후보도 포함, 최초 평가 시점
    스냅샷) — 없으면 trades.entry_reason의 "[조건명] ..." 프리픽스에서 파싱
    (컬럼 추가 이전 과거 데이터 또는 watch_list_log 기록 실패 케이스 대비)."""
    today = date.today()
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (stock_code) stock_code, stock_name, cond_name
                FROM watch_list_log WHERE trade_date = %s
                ORDER BY stock_code, added_time
                """,
                (today,),
            )
            wl_rows = {r["stock_code"]: r for r in cur.fetchall()}

            cur.execute(
                """
                SELECT DISTINCT ON (stock_code) stock_code, stock_name, entry_reason
                FROM trades WHERE trade_date = %s
                ORDER BY stock_code, buy_time
                """,
                (today,),
            )
            tr_rows = {r["stock_code"]: r for r in cur.fetchall()}
    except Exception as e:
        logger.error(f"❌ [백테스트] 오늘 종목 유니버스 조회 실패: {e}")
        return []

    universe = []
    for code in set(wl_rows) | set(tr_rows):
        wl = wl_rows.get(code) or {}
        tr = tr_rows.get(code) or {}
        stock_name = wl.get("stock_name") or tr.get("stock_name") or code
        cond_name = wl.get("cond_name") or ""
        if not cond_name and tr.get("entry_reason"):
            m = re.match(r"^\[(.+?)\]", tr["entry_reason"])
            if m:
                cond_name = m.group(1)
        universe.append(
            {"stock_code": code, "stock_name": stock_name, "cond_name": cond_name}
        )
    return universe


def _entry_signal(candles: list, idx: int, vwap_strategy: VWAPStrategy, cond_name: str = ""):
    """idx 시점(candles[idx]=그 시점 현재봉)에서 서브전략별 진입 판정.
    반환: (sub_strategy, info) 또는 None. **Pullback만 재현** (2026-07-31 변경).

    [1A 재현 중단, 2026-07-31] 1A가 evaluate_1a_leading_strength(체결강도
    100 이상 1분 유지 단독)로 전면 교체되면서, 1A도 이제 1B/1L과 같은 이유
    (틱 단위 체결강도 데이터 필요, 1분봉만으로 재현 불가)로 백테스트 대상에서
    제외한다. 아래 옛 1A 로직(거래량증가지속+score_phase1)은 삭제하지 않고
    주석으로 남겨둠 — 라이브가 다시 옛 방식으로 돌아가면 이 블록도 되살릴 것.

    cond_name 기반 라우팅(2026-08-01 갱신) — core/strategy_manager.py의
    _evaluate_1a_pullback_entry와 동일 규칙:
    ① **상호배타**: cond_name에 "눌림목자동"이 있으면 Pullback 전용,
       없으면 1A 전용. 중복 편입 종목은 Pullback이 우선한다.
    ② 조건검색식별 09:20 지연 게이트는 제거됨(라이브와 동일).
    ③ Pullback 시간창 09:20~15:10(라이브 상수에서 직접 가져옴).
    ④ skip_setup_check=True 유지."""
    sub = candles[idx:]
    if len(sub) < 60:
        return None
    hhmm = _hhmm(sub[0])
    if not (GROUP_A_START_HHMM <= hhmm < PHASE1A_END_HHMM):
        return None

    vol_ratio = _volume_ratio(sub)

    # [주석으로 보류, 2026-07-31] 구 1A(거래량증가지속+score_phase1) — 라이브가
    # evaluate_1a_leading_strength(체결강도 단독, 틱 필요)로 대체되며 더 이상
    # 이 분봉 기반 재현과 대응되지 않음. 삭제하지 않고 보류만 함.
    # pullback_only_source = cond_name == PULLBACK_ONLY_SOURCE
    # if not pullback_only_source and is_volume_increasing_streak(sub):
    #     ok, info = score_phase1(sub, vol_ratio, NEUTRAL_STRENGTH, PHASE1A_CFG)
    #     if ok:
    #         required = (
    #             PHASE1A_SCORE_TIGHT if hhmm >= PHASE1A_TIGHTEN_HHMM else PHASE1A_SCORE_NORMAL
    #         )
    #         if info.get("score", 0) >= required:
    #             return "1A", info

    # Pullback: 09:25~14:50, 눌림목 반등 점수 + VWAP AND
    # (2026-08-01) 라이브 라우팅과 동일하게 재현한다 — 안 맞추면 재현 매매
    # 건수가 실제와 달라져 성과 판단이 틀어진다.
    #   눌림목자동 단독      -> 항상 Pullback
    #   주도주상위/돌파 단독 -> 1A(현재 백테스트 재현 대상 아님)
    #   중복                 -> 10:30 이전은 1A, 이후 Pullback
    # 라이브의 StrategyManager.resolve_strategy()를 그대로 호출해서 규칙이
    # 두 곳에 갈라져 어긋나는 일이 없게 한다.
    from core.strategy_manager import StrategyManager as _SM
    _route = _SM.resolve_strategy(cond_name, _hhmm_to_time(hhmm))
    if _route == "1A_눌림" and PULLBACK_START_HHMM <= hhmm < PULLBACK_END_HHMM:
        ok, info = score_pullback(
            sub, vol_ratio, NEUTRAL_STRENGTH, PULLBACK_CFG,
            skip_setup_check=True,
        )
        if ok:
            vwap = calc_vwap(sub)
            vr = vwap_strategy.evaluate(
                {"price": sub[0]["close"], "vwap": vwap, "candles": sub, "volume_ratio": vol_ratio}
            )
            if vr["bullish"]:
                info["vwap"] = vwap
                info["vwap_gates"] = vr.get("gates")
                return "1A_눌림", info

    return None


def _take_profit_cap(position: dict) -> float:
    """라이브 StrategyManager._take_profit_cap과 동일 규칙 (2026-07-30).
    수치가 갈라지면 백테스트가 라이브를 재현하지 못하므로 함께 갱신할 것."""
    if position.get("buy_hhmm", "") < EARLY_WINDOW_END_HHMM:
        return TAKE_PROFIT_CAP_EARLY
    if position.get("sub_strategy") == "1A_눌림":
        return TAKE_PROFIT_CAP_PULLBACK
    return TAKE_PROFIT_CAP


def _exit_signal(position: dict, current_price: float, hhmm: str, minutes_held: float):
    """라이브 core/strategy_manager.py의 청산 규칙(손절/익절캡/시간정리)을 그대로 재현.
    (2026-08-02) 트레일링은 1L 전용이었고 1L 삭제와 함께 라이브에서도 제거됐다 —
    이제 라이브·백테스트 둘 다 예외 없이 flat 익절캡이라 이 부분은 정확히 일치한다."""
    buy_price = position["buy_price"]
    gross_rate = (current_price - buy_price) / buy_price if buy_price > 0 else 0.0
    net_rate = gross_rate - ROUND_TRIP_COST

    if current_price > position["highest_price"]:
        position["highest_price"] = current_price

    if gross_rate <= STOP_LOSS_RATE:
        return "손절", net_rate

    if net_rate >= _take_profit_cap(position):
        return "익절", net_rate

    if minutes_held >= HOLDING_TIMEOUT.total_seconds() / 60:
        return "시간정리", net_rate

    if hhmm >= FORCE_CLOSE_HHMM:
        return "강제청산", net_rate

    return None, net_rate


def _simulate_stock(
    stock_code: str, stock_name: str, candles: list, vwap_strategy: VWAPStrategy,
    cond_name: str = "",
) -> list:
    """한 종목의 하루치 분봉을 순회하며 진입->청산을 재현.

    재진입 규칙 재현(2026-07-31 수정) — 기존 주석은 "재진입 없음"이었지만
    실제 루프는 청산 후에도 계속 다음 진입을 탐색해 하루 여러 번 매매될 수
    있었고, 그러면서도 라이브의 재진입 제약(쿨다운/손실차단/횟수상한)은
    전혀 반영하지 않아 주석·라이브 둘 다와 어긋나 있었다(예: 씨피시스템이
    하루 4번 매매되는 식으로 과대재현). core/strategy_manager.py._is_rebuy_blocked와
    동일한 3개 규칙을 그대로 재현한다:
      ① 손실 청산(net_rate<0)은 사유(손절/시간정리/강제청산 등) 불문 당일
         재매수 영구 차단
      ② 종목당 하루 최대 MAX_BUYS_PER_STOCK(3)회 (최초 1 + 재매수 2)
      ③ 매도 후 REBUY_COOLDOWN(3분) 이내 재매수 금지 (①에 안 걸린 익절만 해당)
    """
    trades = []
    position = None
    stoploss_blocked = False   # ① 손실 청산 이후 당일 재매수 영구 차단
    buy_count = 0              # ② 당일 매수 횟수 (최초 1 + 재매수 2 = 상한 3)
    sold_at_idx = None         # ③ 마지막 매도 시점 idx (쿨다운 계산용)
    cooldown_min = REBUY_COOLDOWN.total_seconds() / 60

    # run_daily_backtest이 count=FETCH_COUNT(450)로 당겨오는 캔들엔 항상 전일
    # 오후분이 섞여 있는데(하루 거래시간이 390분뿐이라 450개를 채우려면 전일로
    # 넘어감), 아래 hhmm 비교는 날짜를 안 보고 시:분만 비교해서 전일 15:16을
    # "오늘 장마감 이후"로 착각해 시뮬레이션이 시작하자마자(오늘 데이터는 하나도
    # 못 보고) break로 끝나버리고 있었음 — "재현된 매매 없음"의 실제 원인
    # (2026-07-29 실전 확인, 아마 이 백테스트가 도입된 이후 매일 이랬을 것으로
    # 추정). 오늘 날짜 캔들만 남기고 나머지는 버려서 날짜 경계를 명확히 함.
    today_str = date.today().strftime("%Y%m%d")
    candles = [c for c in candles if c.get("time_str", "").startswith(today_str)]

    for idx in range(len(candles) - 1, -1, -1):
        cur = candles[idx]
        hhmm = _hhmm(cur)
        if hhmm < "0900":
            continue
        if hhmm > FORCE_CLOSE_HHMM and position is None:
            break

        if position is None:
            can_reenter = (
                not stoploss_blocked
                and buy_count < MAX_BUYS_PER_STOCK
                and (sold_at_idx is None or (sold_at_idx - idx) >= cooldown_min)
            )
            if can_reenter:
                sig = _entry_signal(candles, idx, vwap_strategy, cond_name)
                if sig:
                    sub_strategy, info = sig
                    buy_count += 1
                    position = {
                        "sub_strategy": sub_strategy,
                        "buy_price": cur["close"],
                        "buy_hhmm": hhmm,
                        "buy_idx": idx,
                        "highest_price": cur["close"],
                        "info": info,
                    }
        else:
            minutes_held = position["buy_idx"] - idx  # 1분봉이므로 인덱스 차 = 경과분
            reason, net_rate = _exit_signal(position, cur["close"], hhmm, minutes_held)
            if reason:
                trades.append(
                    {
                        "stock_code": stock_code,
                        "stock_name": stock_name,
                        "cond_name": cond_name or "미상",
                        "sub_strategy": position["sub_strategy"],
                        "buy_price": position["buy_price"],
                        "buy_time": position["buy_hhmm"],
                        "sell_price": cur["close"],
                        "sell_time": hhmm,
                        "exit_reason": reason,
                        "net_rate": net_rate,
                    }
                )
                if net_rate < 0:
                    stoploss_blocked = True
                sold_at_idx = idx
                position = None

    return trades


def _format_report(trades: list, universe_count: int, skipped: int) -> str:
    if not trades:
        return (
            f"📊 일일 백테스트 결과 ({date.today()})\n"
            f"대상 종목 {universe_count}개 (분봉 조회 실패 {skipped}개) — 재현된 매매 없음"
        )

    total = len(trades)
    wins = [t for t in trades if t["net_rate"] > 0]
    win_rate = len(wins) / total * 100
    avg_rate = sum(t["net_rate"] for t in trades) / total * 100

    lines = [
        f"📊 일일 백테스트 결과 ({date.today()})",
        "⚠️ 참고: 2026-08-02부터 라이브 1A/Pullback은 **틱 구동**",
        "(체결강도 3초 연속 + 대량체결 버스트)으로 바뀌었습니다.",
        "아래 수치는 분봉 기준 눌림목 재현이라 **실제 진입과 다릅니다** —",
        "청산 정책 비교용으로만 보세요.",
        "",
        f"대상 종목 {universe_count}개 / 재현 매매 {total}건",
        f"전체 승률 {win_rate:.1f}% | 평균수익률 {avg_rate:+.2f}%",
        "",
        "[전략별 성과]",
    ]

    by_strategy = defaultdict(list)
    for t in trades:
        by_strategy[t["sub_strategy"]].append(t)

    for sub, ts in sorted(by_strategy.items()):
        n = len(ts)
        w = sum(1 for t in ts if t["net_rate"] > 0)
        avg = sum(t["net_rate"] for t in ts) / n * 100
        reason_cnt = defaultdict(int)
        for t in ts:
            reason_cnt[t["exit_reason"]] += 1
        reason_str = ", ".join(
            f"{r} {c}" for r, c in sorted(reason_cnt.items(), key=lambda x: -x[1])
        )
        lines.append(f"  {sub}: {n}건, 승률 {w/n*100:.0f}%, 평균 {avg:+.2f}% ({reason_str})")

    # 조건검색식별 성과 (2026-07-31 신규) — "검색식과 로직이 잘 맞았는지"를
    # 직접 확인하기 위한 구간. cond_name은 최초 평가 시점 스냅샷(watch_list_log)
    # 또는 trades.entry_reason 파싱 값 — 컬럼 추가 이전 종목은 "미상"으로 표시.
    lines.append("")
    lines.append("[조건검색식별 성과]")
    by_cond = defaultdict(list)
    for t in trades:
        by_cond[t.get("cond_name", "미상")].append(t)
    for cond, ts in sorted(by_cond.items()):
        n = len(ts)
        w = sum(1 for t in ts if t["net_rate"] > 0)
        avg = sum(t["net_rate"] for t in ts) / n * 100
        lines.append(f"  {cond}: {n}건, 승률 {w/n*100:.0f}%, 평균 {avg:+.2f}%")

    lines.append("")
    lines.append("[종목별 내역]")
    for t in sorted(trades, key=lambda x: x["net_rate"], reverse=True):
        buy_hhmm = f"{t['buy_time'][:2]}:{t['buy_time'][2:]}"
        sell_hhmm = f"{t['sell_time'][:2]}:{t['sell_time'][2:]}"
        lines.append(
            f"  {t['stock_name']}({t['stock_code']}) [{t['sub_strategy']}/{t.get('cond_name', '미상')}] "
            f"{buy_hhmm}~{sell_hhmm} {t['buy_price']:,.0f}→{t['sell_price']:,.0f}원 "
            f"{t['net_rate']*100:+.2f}% ({t['exit_reason']})"
        )

    lines.append("")
    lines.append(
        "⚠️ 한계: 체결강도/OBV는 틱데이터 없어 중립값 고정(1B/1L 자체는 미재현). "
        "동적 익절캡 상향·조기확정·즉시매도·손실반등 로직도 강도 데이터가 필요해 "
        "미재현 — 단, 이 로직들이 강도 데이터 부재 시 라이브에서도 기본 캡을 그대로 "
        "유지하는 것과 동일해 결과가 크게 갈리진 않음."
    )

    return "\n".join(lines)


def _send_report(text: str, target: str = "order"):
    """텔레그램 메시지 길이 제한 대비 청크 전송."""
    CHUNK = 3500
    if len(text) <= CHUNK:
        send_telegram(text, target=target)
        return
    parts = [text[i : i + CHUNK] for i in range(0, len(text), CHUNK)]
    for i, part in enumerate(parts, 1):
        send_telegram(f"[{i}/{len(parts)}]\n{part}", target=target)


def run_daily_backtest(rest_api):
    """오늘 신호 종목 대상 백테스트 실행 + 텔레그램(오토트레이더) 전송."""
    logger.info("🔄 [백테스트] 일일 백테스트 시작...")
    universe = _get_today_universe()
    if not universe:
        logger.info("[백테스트] 오늘 신호 종목 없음 — 종료")
        _send_report(f"📊 일일 백테스트 ({date.today()}): 오늘 신호 종목 없음")
        return

    vwap_strategy = VWAPStrategy()
    all_trades = []
    skipped = 0

    for row in universe:
        code = row["stock_code"]
        name = row.get("stock_name") or code
        try:
            candles = rest_api.get_minute_candles(code, interval=1, count=FETCH_COUNT)
        except Exception as e:
            logger.warning(f"[백테스트] [{code}] 분봉 조회 실패: {e}")
            skipped += 1
            continue
        if not candles:
            skipped += 1
            continue
        try:
            trades = _simulate_stock(code, name, candles, vwap_strategy, row.get("cond_name", ""))
            all_trades.extend(trades)
        except Exception:
            logger.exception(f"[백테스트] [{code}] 시뮬레이션 실패")

    report = _format_report(all_trades, len(universe), skipped)
    logger.info("[백테스트] 완료: %d건 재현, 리포트 전송", len(all_trades))
    _send_report(report)
