"""
슬롯 교체 로직 — 체결강도 하락 + 거래량 정체 종목을 감시종목 중
점수 높은 후보로 교체. 손절과 동일한 _execute_sell 경로를 태워서
심플하게 처리한다. (2026-07-26 신규)
"""

from utils.logger import logger

STAGNANT_MIN_HOLD_MINUTES = 10        # 최소 보유 시간 (이전엔 교체 후보 자격 없음)
STRENGTH_DECLINE_RATIO = 0.8          # 현재강도 < 진입강도 * 이 값이면 "하락"
VOLUME_STAGNANT_RATIO = 1.0           # volume_ratio < 이 값이면 "정체"
CANDIDATE_SCORE_MARGIN = 1.2          # 후보점수 >= 정체종목점수 * 이 값이어야 교체
MAX_SLOT_REPLACEMENTS_PER_DAY = 40


def find_stagnant_holding(strat, now) -> tuple[str, dict] | None:
    """holdings 중 교체 대상 자격이 되는 종목 하나를 찾아 반환 (없으면 None).
    자격: 매수 후 10분 경과 + 체결강도 하락 + 거래량 정체."""
    for code, pos in list(strat.holdings.items()):
        buy_time = pos.get("buy_time")
        if not buy_time:
            continue
        held_minutes = (now - buy_time).total_seconds() / 60
        if held_minutes < STAGNANT_MIN_HOLD_MINUTES:
            continue

        entry_strength = pos.get("entry_strength")
        if entry_strength is None or entry_strength <= 0:
            continue

        try:
            current_strength = strat._current_strength(code)
        except Exception as e:
            logger.warning("[%s] 슬롯교체 강도조회 실패: %s", code, e)
            continue

        if current_strength >= entry_strength * STRENGTH_DECLINE_RATIO:
            continue  # 강도 하락 아님

        try:
            candles = strat._get_merged_candles(code, interval=1, count=30)
            volume_ratio = strat._volume_ratio(candles) if candles else None
        except Exception as e:
            logger.warning("[%s] 슬롯교체 거래량조회 실패: %s", code, e)
            continue

        if volume_ratio is None or volume_ratio >= VOLUME_STAGNANT_RATIO:
            continue  # 거래량 정체 아님

        return code, {
            "held_minutes": held_minutes,
            "entry_strength": entry_strength,
            "current_strength": current_strength,
            "volume_ratio": volume_ratio,
        }
    return None


def find_replacement_candidate(strat, stagnant_score: float) -> tuple[str, float] | None:
    """watch_list_today 중 미보유·미대기 종목 중 점수가 stagnant_score*margin
    이상인 최고점 후보를 찾아 반환 (없으면 None)."""
    best_code, best_score = None, 0.0
    threshold = stagnant_score * CANDIDATE_SCORE_MARGIN
    for code in strat.watch_list_today:
        if code in strat.holdings or code in strat.pending:
            continue
        blocked, _ = strat._is_rebuy_blocked(code)
        if blocked:
            continue  # 오늘 손실 청산(또는 쿨다운 중)된 종목은 대체후보 자격 없음
        score = strat._watch_scores.get(code)
        if score is None or score < threshold:
            continue
        if score > best_score:
            best_code, best_score = code, score
    if best_code is None:
        return None
    return best_code, best_score


def try_slot_replacement(strat, send_telegram, replacement_count: int, now) -> int:
    """슬롯 교체 1회 시도. 반환값: 갱신된 replacement_count (교체 없었으면 그대로)."""
    if replacement_count >= MAX_SLOT_REPLACEMENTS_PER_DAY:
        return replacement_count

    found = find_stagnant_holding(strat, now)
    if not found:
        return replacement_count
    stagnant_code, detail = found

    pos = strat.holdings.get(stagnant_code)
    if not pos:
        return replacement_count
    stagnant_score = pos.get("entry_score", 0.0)

    candidate = find_replacement_candidate(strat, stagnant_score)
    if not candidate:
        return replacement_count
    candidate_code, candidate_score = candidate

    stagnant_name = pos.get("stock_name", stagnant_code)
    candidate_name = strat._stock_names.get(candidate_code, candidate_code)

    exit_reason = (
        f"슬롯 교체: 체결강도 하락({detail['entry_strength']:.0f}->{detail['current_strength']:.0f}) "
        f"+ 거래량 정체(x{detail['volume_ratio']:.2f}) "
        f"| 보유 {detail['held_minutes']:.0f}분 "
        f"-> 대체후보 {candidate_name}({candidate_code}) 점수 {candidate_score:.1f}"
    )

    current_price = pos.get("buy_price")
    try:
        candles = strat._get_merged_candles(stagnant_code, interval=1, count=1)
        if candles:
            current_price = candles[0].get("close", current_price)
    except Exception:
        pass

    logger.info("[%s] %s", stagnant_code, exit_reason)
    strat._execute_sell(stagnant_code, current_price, exit_reason)

    if send_telegram:
        send_telegram(
            f"🔄 슬롯 교체\n"
            f"매도: {stagnant_name} ({stagnant_code})\n"
            f"사유: {exit_reason}\n"
            f"오늘 교체 {replacement_count + 1}/{MAX_SLOT_REPLACEMENTS_PER_DAY}회",
            target="order",
        )

    return replacement_count + 1