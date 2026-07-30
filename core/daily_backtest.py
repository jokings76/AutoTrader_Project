"""
일일 백테스트 — 그날 실제 조건검색/테마 신호를 받은 종목(watch_list_log + trades)을
대상으로, 라이브와 동일한 진입 스코어링 함수(core/strategy/scoring.py, vwap_strategy.py)와
청산 정책 상수(core/strategy_manager.py)를 그대로 재사용해 분봉 기준으로 매매를 재현한다.
(2026-07-27: 1S/1N/Phase2/Phase3 삭제된 신규 전략 구조에 맞춰 갱신 — 1A/Pullback만 재현)

한계 (심플하게 유지하기 위한 의도적 단순화):
  - 1B(체결강도 FSM), 1L(주도주 실시간강도 2분지속)는 틱/호가 데이터가 없으면 재현 불가 → 제외.
  - 체결강도(current_strength)는 분봉만으로 알 수 없어 중립값 100.0 고정
    (라이브에서 phase1b 없을 때 쓰는 fallback과 동일). 1A의 체결강도 지속 필터, 지수
    방어 CAUTION 가산점도 같은 이유로 재현하지 않고 시간대 기준 점수 컷만 반영.
  - 종목별 독립 시뮬레이션 (슬롯/동시보유 한도 미반영) — 진입/청산 규칙 자체의
    유효성을 보는 게 목적이라 자금 배분 제약은 넣지 않음.

실행: 이 모듈은 main.py의 태스크에서 run_daily_backtest(rest_api)로 호출됨.
"""
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
)

NEUTRAL_STRENGTH = 100.0  # 틱데이터 없어 체결강도는 중립값 고정
FORCE_CLOSE_HHMM = "1515"  # 라이브 FORCE_CLOSE_TIME과 동일
FETCH_COUNT = 450  # 09:00~15:20 + 60MA 여유
GROUP_A_START_HHMM = "0901"
PULLBACK_END_HHMM = "1030"
PHASE1A_TIGHTEN_HHMM = "1030"
PHASE1A_END_HHMM = "1450"
EARLY_WINDOW_END_HHMM = EARLY_WINDOW_END.strftime("%H%M")  # 개장초반 익절 1.5% 경계

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
    """오늘 watch_list_log + trades에 기록된 종목(그날 실제 신호/매매 종목) 유니버스."""
    today = date.today()
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT stock_code, stock_name FROM watch_list_log WHERE trade_date = %s
                UNION
                SELECT DISTINCT stock_code, stock_name FROM trades WHERE trade_date = %s
                """,
                (today, today),
            )
            return cur.fetchall()
    except Exception as e:
        logger.error(f"❌ [백테스트] 오늘 종목 유니버스 조회 실패: {e}")
        return []


def _entry_signal(candles: list, idx: int, vwap_strategy: VWAPStrategy):
    """idx 시점(candles[idx]=그 시점 현재봉)에서 서브전략별 진입 판정.
    반환: (sub_strategy, info) 또는 None. 1A/Pullback만 재현 (1B/1L은 틱데이터 필요해 제외)."""
    sub = candles[idx:]
    if len(sub) < 60:
        return None
    hhmm = _hhmm(sub[0])
    if not (GROUP_A_START_HHMM <= hhmm < PHASE1A_END_HHMM):
        return None
    vol_ratio = _volume_ratio(sub)

    # 1A: 거래량증가지속 + score_phase1, 10:30부터 점수 커트라인 상향
    if is_volume_increasing_streak(sub):
        ok, info = score_phase1(sub, vol_ratio, NEUTRAL_STRENGTH, PHASE1A_CFG)
        if ok:
            required = (
                PHASE1A_SCORE_TIGHT if hhmm >= PHASE1A_TIGHTEN_HHMM else PHASE1A_SCORE_NORMAL
            )
            if info.get("score", 0) >= required:
                return "1A", info

    # Pullback: 09:01~10:30, 눌림목 반등 점수 + VWAP AND
    if hhmm < PULLBACK_END_HHMM:
        ok, info = score_pullback(sub, vol_ratio, NEUTRAL_STRENGTH, PULLBACK_CFG)
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
    트레일링은 1L 전용인데 1L은 백테스트에서 재현 안 하므로 항상 flat 익절캡."""
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


def _simulate_stock(stock_code: str, stock_name: str, candles: list, vwap_strategy: VWAPStrategy) -> list:
    """한 종목의 하루치 분봉을 순회하며 진입->청산 1회 재현 (재진입 없음, 심플 유지)."""
    trades = []
    position = None

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
            sig = _entry_signal(candles, idx, vwap_strategy)
            if sig:
                sub_strategy, info = sig
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
                        "sub_strategy": position["sub_strategy"],
                        "buy_price": position["buy_price"],
                        "buy_time": position["buy_hhmm"],
                        "sell_price": cur["close"],
                        "sell_time": hhmm,
                        "exit_reason": reason,
                        "net_rate": net_rate,
                    }
                )
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

    lines.append("")
    lines.append("[종목별 내역]")
    for t in sorted(trades, key=lambda x: x["net_rate"], reverse=True):
        buy_hhmm = f"{t['buy_time'][:2]}:{t['buy_time'][2:]}"
        sell_hhmm = f"{t['sell_time'][:2]}:{t['sell_time'][2:]}"
        lines.append(
            f"  {t['stock_name']}({t['stock_code']}) [{t['sub_strategy']}] "
            f"{buy_hhmm}~{sell_hhmm} {t['buy_price']:,.0f}→{t['sell_price']:,.0f}원 "
            f"{t['net_rate']*100:+.2f}% ({t['exit_reason']})"
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
            trades = _simulate_stock(code, name, candles, vwap_strategy)
            all_trades.extend(trades)
        except Exception:
            logger.exception(f"[백테스트] [{code}] 시뮬레이션 실패")

    report = _format_report(all_trades, len(universe), skipped)
    logger.info("[백테스트] 완료: %d건 재현, 리포트 전송", len(all_trades))
    _send_report(report)
