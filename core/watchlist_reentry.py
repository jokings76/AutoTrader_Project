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
"""

from utils.logger import logger
from core.strategy_manager import GROUP_A_START, PHASE1A_END, VOLUME_LOOKBACK


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
        cond_name = strat._cond_names.get(code, "")
        is_pullback_source = "눌림목자동" in cond_name

        candles = None
        current_price = 0
        open_price = 0.0

        if not is_pullback_source:
            # ── 1A 고속 경로: REST 0콜 ──────────────────────────────
            # 조건: ① 최근 체결가가 신선하고 ② 당일 시가가 이미 캐시돼 있을 것.
            # 시가 캐시가 없으면(장 시작 전에 편입돼 아직 분봉을 한 번도 안
            # 받은 종목 등) 아래 REST 경로로 내려가 한 번만 채우고, 그 다음
            # 사이클부터는 이 고속 경로를 탄다(자가 치유).
            # 시가를 0으로 두고 그냥 진행하면 "주도주상위 시가대비 +5% 보류"
            # 필터가 조용히 꺼져버리므로 절대 생략하지 않는다.
            px = strat._fresh_tick_price(code)
            cached_open = strat._opening_prices.get(code)
            if px and cached_open:
                current_price = int(px)
                open_price = float(cached_open)

        if not current_price:
            # ── 기존 REST 경로 (Pullback 또는 캐시 미비 시) ───────────
            try:
                candles = strat._get_merged_candles(code, interval=1, count=15)
            except Exception as e:
                logger.warning("[%s] watchlist 재평가 분봉조회 실패: %s", code, e)
                continue
            if not candles or len(candles) < VOLUME_LOOKBACK + 1:
                continue

            current_price = int(candles[0].get("close", 0))
            # 당일 시가 — candles[-1]["open"]은 내림차순이라 '가장 오래된 봉'이고
            # 개장 직후엔 전일 봉이 섞여 들어와 사실상 전일 시가였다(2026-08-01 수정,
            # on_condition_hit과 동일한 헬퍼를 써서 두 경로가 항상 같은 값을 보게 함).
            open_price = strat._today_open(candles)
            # 시가는 하루 내내 불변 — 여기서 캐시해두면 다음 사이클부터 REST가 0이 된다.
            if open_price > 0:
                strat._opening_prices.setdefault(code, open_price)

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
