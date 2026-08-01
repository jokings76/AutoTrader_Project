"""슬롯이 꽉 차서 매수 못 했던 1A/Pullback 후보 재평가 — 슬롯이 빌 때마다 감시 재개.
(2026-07-28 신규) on_condition_hit()은 종목당 최초 편입 시점에 딱 한 번만 평가하기
때문에, 그 순간 점수는 통과했는데 슬롯이 없으면 watch_list에 기록만 되고 다시는
재평가가 안 되던 문제를 해결한다. 1B/1L은 실시간 틱 콜백이라 원래도 슬롯이 빌 때까지
계속 재시도하므로 대상에서 제외.

슬롯이 실제로 비어있을 때만 동작(REST 낭비 방지) — can_buy_phase1a()/can_buy_pullback()
둘 다 막혀 있으면 아무 것도 안 하고 즉시 반환.

(2026-08-01) REST 무호출 경로 추가 — 새 1A(evaluate_1a_leading_strength)는
체결틱만으로 판정하므로 분봉이 전혀 필요 없다. 실시간 체결가와 캐시된 당일
시가가 있으면 REST를 한 번도 부르지 않고 평가한다. 이 루프는 15초마다 후보
전체를 훑기 때문에, 후보가 40종목이면 예전 방식으로는 한 사이클에 REST 40콜
(자체 상한 분당 100콜의 절반 이상)을 태웠다 — 실시간 조건검색 편입 유실을
고치면서 후보 수가 크게 늘어난 만큼 이 경로가 없으면 429가 급증한다.

(2026-08-02) **이 태스크의 역할이 主에서 안전망으로 내려갔다.** 진입 평가가
on_trade(체결 틱 콜백)로 옮겨가면서 실제 진입은 거의 전부 거기서 일어난다
— 3초짜리 신호를 15초 주기로 들여다보던 구조가 근본 원인이었기 때문이다
(평균 7.5초 지연 + 짧게 스치는 신호는 관측조차 불가).
여기 남겨두는 이유는 틱이 끊긴 구간(WS 재연결 직후 등)에서 슬롯이 비었을 때
후보를 다시 훑어주는 백스톱 역할 때문이다. Pullback도 틱 구동으로 바뀌어
이제 두 전략 모두 REST를 한 콜도 쓰지 않는다.
"""

from utils.logger import logger
from core.strategy_manager import GROUP_A_START, ENTRY_WINDOW_END


def try_watchlist_reentry(strat, now) -> int:
    """watch_list_today 중 미보유·미대기 종목을 슬롯 여유가 있을 때만 재평가.
    반환: 이번 호출에서 매수까지 성공한 종목 수."""
    now_t = now.time()
    # (2026-08-01) PHASE1A_END(14:50) -> ENTRY_WINDOW_END(1A/Pullback 중 늦은 쪽).
    # Pullback을 15:10까지 늘렸는데 이 게이트가 14:50에 먼저 끊기면 마지막
    # 20분 동안 재평가가 멈춰 사실상 창이 안 늘어난다.
    if not (GROUP_A_START <= now_t < ENTRY_WINDOW_END):
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

        # ── REST 0콜 경로 (2026-08-02: 1A/Pullback 공통) ────────────────
        # 예전엔 Pullback만 분봉(REST)을 받아야 했는데, Pullback이 틱 구동으로
        # 전환되면서 두 전략 모두 체결틱만으로 판정한다. 이 루프는 15초마다
        # 후보 전체를 훑기 때문에 후보가 40종목이면 예전엔 한 사이클에
        # REST 40콜(자체 상한 분당 100콜의 절반 가까이)을 태웠다 — 이제 0콜이다.
        #
        # 실시간 체결가가 없으면(그 종목에 최근 틱이 없음) 재평가할 근거 자체가
        # 없으므로 조용히 건너뛴다. 어차피 틱이 없으면 무장(강도 3초 연속)도
        # 성립할 수 없다.
        px = strat._fresh_tick_price(code)
        if not px:
            continue
        current_price = int(px)
        # 시가는 하루 내내 불변이라 pre-arm이 채워둔 캐시를 그대로 쓴다.
        # 없으면 0 -> "시가대비 +5% 보류" 필터만 건너뛴다(모르는 값으로
        # 매수를 막지 않는다. 그 필터는 1A 전용 보조 게이트다).
        open_price = float(strat._opening_prices.get(code, 0.0))

        try:
            executed = strat._evaluate_1a_pullback_entry(
                code, stock_name, 1, None, current_price, open_price, now_t
            )
        except Exception as e:
            logger.warning("[%s] watchlist 재평가 실패: %s", code, e)
            continue

        if executed:
            bought += 1
            logger.info("[%s] %s watchlist 재진입 매수 성공 (슬롯 재확보)", code, stock_name)

    return bought
