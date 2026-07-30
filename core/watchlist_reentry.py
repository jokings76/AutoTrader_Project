"""
슬롯이 꽉 차서 매수 못 했던 1A/Pullback 후보 재평가 — 슬롯이 빌 때마다 감시 재개.
(2026-07-28 신규) on_condition_hit()은 종목당 최초 편입 시점에 딱 한 번만 평가하기
때문에, 그 순간 점수는 통과했는데 슬롯이 없으면 watch_list에 기록만 되고 다시는
재평가가 안 되던 문제를 해결한다. 1B/1L은 실시간 틱 콜백이라 원래도 슬롯이 빌 때까지
계속 재시도하므로 대상에서 제외.

슬롯이 실제로 비어있을 때만 동작(REST 낭비 방지) — can_buy_phase1a()/can_buy_pullback()
둘 다 막혀 있으면 아무 것도 안 하고 즉시 반환.
"""

from utils.logger import logger
from core.strategy_manager import GROUP_A_START, PHASE1A_END


def try_watchlist_reentry(strat, now) -> int:
    """watch_list_today 중 미보유·미대기 종목을 슬롯 여유가 있을 때만 재평가.
    반환: 이번 호출에서 매수까지 성공한 종목 수."""
    now_t = now.time()
    if not (GROUP_A_START <= now_t < PHASE1A_END):
        return 0  # on_condition_hit과 동일한 시간대 게이트

    # 확장 슬롯(2026-07-31)이 열려있을 수 있는 상태면 평상시 상한이 꽉 찼어도
    # 재평가를 진행한다 — 실제 허용 여부는 후보 점수를 보는 can_buy_*(info)가
    # 판정한다. 이 사전확인을 빼면 '만석이라 조기 반환' 때문에 확장 슬롯이
    # 사실상 신규 편입 이벤트에서만 열려 의도대로 동작하지 않는다.
    if not (strat.can_buy_phase1a() or strat.can_buy_pullback()
            or strat.may_expand_slots()):
        return 0  # 슬롯 없으면 REST 호출도 안 함

    bought = 0

    for code in list(strat.watch_list_today):
        if code in strat.holdings or code in strat.pending:
            continue
        if not (strat.can_buy_phase1a() or strat.can_buy_pullback()
                or strat.may_expand_slots()):
            break  # 재평가 도중 슬롯이 다 찼으면 중단

        stock_name = strat._stock_names.get(code, code)
        try:
            candles = strat._get_merged_candles(code, interval=1, count=15)
        except Exception as e:
            logger.warning("[%s] watchlist 재평가 분봉조회 실패: %s", code, e)
            continue
        if not candles or len(candles) < 6:
            continue

        current_price = int(candles[0].get("close", 0))
        open_price = int(candles[-1].get("open", 0))

        try:
            executed = strat._evaluate_1a_pullback_entry(
                code, stock_name, 1, candles, current_price, open_price, now_t
            )
        except Exception as e:
            logger.warning("[%s] watchlist 재평가 실패: %s", code, e)
            continue

        if executed:
            bought += 1
            logger.info("[%s] %s watchlist 재진입 매수 성공 (슬롯 재확보)", code, stock_name)

    return bought
