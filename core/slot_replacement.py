"""
슬롯 교체 로직 — 체결강도 하락 + 거래량 정체 종목을 감시종목 중
점수 높은 후보로 교체. 손절과 동일한 _execute_sell 경로를 태워서
심플하게 처리한다. (2026-07-26 신규)
"""

from core.strategy.trade_flow import STRENGTH_NEUTRAL
# MAX_HOLDINGS는 슬롯 만석 판정에 쓴다. strategy_manager는 이 모듈을
# 임포트하지 않으므로(주석 언급만 있음) 순환 임포트 위험이 없다.
from core.strategy_manager import MAX_HOLDINGS
from utils.logger import logger

STAGNANT_MIN_HOLD_MINUTES = 10        # 최소 보유 시간 (이전엔 교체 후보 자격 없음)
STRENGTH_DECLINE_RATIO = 0.8          # 현재강도 < 진입강도 * 이 값이면 "하락"
VOLUME_STAGNANT_RATIO = 1.0           # volume_ratio < 이 값이면 "정체"
CANDIDATE_SCORE_MARGIN = 1.2          # 후보점수 >= 정체종목점수 * 이 값이어야 교체
MAX_SLOT_REPLACEMENTS_PER_DAY = 40


def find_stagnant_holding(strat, now) -> tuple[str, dict] | None:
    """holdings 중 교체 대상 자격이 되는 종목들을 모두 찾아, 그중 우선순위가
    가장 높은 종목 하나를 반환 (없으면 None).

    자격(기존 유지): 매수 후 10분 경과 + 체결강도 하락 + 거래량 정체.
    자격 판정을 AND로 두는 건 의도된 설계 — 동적 익절캡에서 같은 하락 판정을
    OR로 바꿨더니 과민하게 이탈해 성과가 악화된 사례가 있었다(2026-07-30
    백테스트: 건당 -0.328% -> -0.532%).

    우선순위(2026-07-31 사용자 지정): 자격 종목 중 **손실이 덜 난 순서**.
    같은 정체 종목이라도 -0.3%를 잘라내는 것과 -2.5%를 잘라내는 것은 부담이
    전혀 다르다 — 크게 밀린 종목은 손실을 확정시키는 비용이 크고 되돌림
    여지도 남아있으므로, 손실이 얕은 쪽부터 교체해 실현손실을 최소화한다.
    (깊게 밀린 종목은 손절 -3% 또는 손실반등 하이브리드 매도가 따로 담당)"""
    candidates: list[tuple[str, dict]] = []

    for code, pos in list(strat.holdings.items()):
        if code in strat.pending or code in strat.sell_blocked:
            continue  # 이미 매도 진행 중 — REST 낭비 방지
        buy_time = pos.get("buy_time")
        if not buy_time:
            continue
        held_minutes = (now - buy_time).total_seconds() / 60
        if held_minutes < STAGNANT_MIN_HOLD_MINUTES:
            continue

        # ⚠️ (2026-08-03) `entry_strength`를 그대로 쓰면 안 된다.
        # 진입은 대량체결 버스트가 터지는 그 순간에 일어나므로, 그때 캡처한
        # compute_strength(최근 10초 창)에는 그 버스트가 통째로 들어가 거의 항상
        # **국소 최고점**이다. 그 값을 기준으로 `현재 < 기준 x 0.8`을 재면
        # **정상 수준으로 돌아오기만 해도 '하락'**이 되어, 교체 대상 선정이
        # 구조적으로 남발된다.
        # 08-03 실측이 그대로 보여준다 — 교체된 3종목의 진입강도가
        # 090710 **300**(compute_strength 상한 포화) / 336260 100 / 319400 70이고
        # 현재강도는 5 / 9 / 45였다. 300에서 시작하면 하락 판정을 피할 수 없다.
        # 같은 결함을 오늘 _update_dynamic_caps와 _is_strength_rising_vs_entry
        # 에서는 고쳤는데 **여기만 빠져 있었다**(같은 규칙이 세 곳에 흩어져 있어
        # 두 곳만 고친 전형적인 사고).
        # -> 워밍업 종료 후 안정화된 기준선으로 교체. 기준선을 못 잡았으면
        #    비교 자체를 포기한다(옛 스파이크로 폴백하면 수정이 무의미해진다).
        try:
            strat._maybe_anchor_strength_baseline(pos, code)
            entry_strength = strat._strength_baseline(pos)
        except Exception:
            entry_strength = 0.0
        if not entry_strength or entry_strength <= 0:
            continue  # 기준선 미확보 -> 판단 불가(보수적으로 유지)

        try:
            current_strength = strat._current_strength(code)
        except Exception as e:
            logger.warning("[%s] 슬롯교체 강도조회 실패: %s", code, e)
            continue

        # 중립값(STRENGTH_NEUTRAL=100.0)은 '틱 부족으로 판단 불가'라는 뜻이지
        # '강도 하락'이 아니다 (2026-08-01 추가). 07-31에 이 구분이 빠진
        # _update_dynamic_caps가 매수 66초 만에 멀쩡한 포지션을 잘라낸 사고가
        # 있었고 거기엔 가드를 넣었는데 여기만 빠져 있었다 — 진입강도가 100을
        # 넘는 대부분의 포지션에서 '데이터 없음'이 곧바로 '하락'으로 오인된다.
        if current_strength <= 0 or current_strength == STRENGTH_NEUTRAL:
            continue  # 판단 불가 -> 보수적으로 유지
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

        # 우선순위 정렬용 순손익 — 위에서 이미 받아온 분봉을 재사용해서
        # REST 추가 호출 없이 현재가를 얻는다(캔들 없으면 매수가로 폴백=0%).
        cur_price = pos.get("buy_price")
        if candles:
            cur_price = candles[0].get("close", cur_price) or cur_price
        try:
            net_rate = strat._net_rate(pos.get("buy_price"), cur_price)
        except Exception:
            net_rate = 0.0

        candidates.append((code, {
            "held_minutes": held_minutes,
            "entry_strength": entry_strength,
            "current_strength": current_strength,
            "volume_ratio": volume_ratio,
            "net_rate": net_rate,
            "current_price": cur_price,
        }))

    if not candidates:
        return None

    # 손실이 덜 난(순손익이 큰) 종목 우선
    candidates.sort(key=lambda item: item[1]["net_rate"], reverse=True)
    if len(candidates) > 1:
        logger.info(
            "슬롯교체 후보 %d종목 — 손실 얕은 순: %s",
            len(candidates),
            ", ".join(f"{c}({d['net_rate']*100:+.2f}%)" for c, d in candidates),
        )
    return candidates[0]


# (2026-08-03) 대체후보는 '지금 무장 중'이어야 한다.
#   True  = 무장 중인 후보만 교체 자격 (기본, 권장)
#   False = 옛 동작(저장된 점수만으로 선정) — 되돌릴 때만 쓸 것
REQUIRE_CANDIDATE_ARMED = True
# (2026-08-03 사용자 지정) 무장에 더해 **버스트까지 지금 성립**해야 한다.
# 즉 "슬롯만 비면 즉시 매수될 자리"만 교체 사유가 된다 — 파는 순간과 사는
# 순간 사이의 간극을 최소화하는 것이 목적이다.
#   ⚠️ 부작용 인지: 버스트 창은 5초인데 교체 태스크는 60초 주기라, 폴링이
#   버스트와 겹칠 확률이 낮다. 즉 이 조건을 켜면 슬롯 교체가 **매우 드물게**
#   일어난다(사실상 거의 안 돎). 08-03처럼 근거 없이 파는 것보다는 낫지만,
#   교체를 제대로 살리려면 60초 폴링이 아니라 **틱 진입 경로에서 '완전히
#   준비됐는데 슬롯만 없음'을 감지한 순간** 교체하는 구조가 맞다(이월 과제).
REQUIRE_CANDIDATE_BURST = True


def find_replacement_candidate(strat, stagnant_score: float) -> tuple[str, float] | None:
    """watch_list_today 중 미보유·미대기 종목 중 점수가 stagnant_score*margin
    이상이고 **지금 무장 중인** 최고점 후보를 반환 (없으면 None).

    (2026-08-01) 여기서 다루는 '점수'는 전부 **컷라인 대비 비율**이다
    (1.0 = 자기 전략 컷라인 정확히 충족). 1A는 체결강도(0~300),
    Pullback은 점수(0~9)로 스케일이 달라 원점수 비교가 무의미했다.

    ⚠️ (2026-08-03) **무장 조건 추가.** 점수만 보면 08-02 틱 전환 이후엔
    '살 수 없는 종목'을 근거로 보유분을 팔게 된다 — 점수는 과거 평가 시점의
    저장값인데 실제 매수는 지금 이 순간의 무장+버스트를 요구하기 때문이다.
    08-03 실사례: 교체 3건이 전부 950160을 근거로 팔았는데 그 종목은 하루 종일
    매수되지 않았고(무장 9회 전부 11:06~11:23, 교체는 12:00 이후라 시각도
    불일치), 235,860원만 실현손실로 확정됐다.
    """
    best_code, best_score = None, 0.0
    # 정체 종목의 점수를 모르면(0) 문턱이 0이 되어 아무 후보나 통과했다.
    # 비율 스케일에서 '보통 수준'인 1.0을 기준으로 삼아, 최소한 자기
    # 컷라인의 1.2배는 되는 후보만 교체 자격을 갖게 한다. (2026-08-01)
    base = stagnant_score if stagnant_score and stagnant_score > 0 else 1.0
    threshold = base * CANDIDATE_SCORE_MARGIN
    for code in strat.watch_list_today:
        if code in strat.holdings or code in strat.pending:
            continue
        blocked, _ = strat._is_rebuy_blocked(code)
        if blocked:
            continue  # 오늘 손실 청산(또는 쿨다운 중)된 종목은 대체후보 자격 없음
        score = strat._watch_scores.get(code)
        if score is None or score < threshold:
            continue
        # 점수를 통과해도 '지금 살 수 있는 상태'가 아니면 자격 없음.
        # 판정 실패(예외/미구현)는 **자격 없음**으로 본다 — 여기서 관대하게
        # 넘기면 이 수정 자체가 무의미해진다(팔고 못 사는 게 원래 문제였다).
        if REQUIRE_CANDIDATE_ARMED:
            try:
                if not strat.is_armed_now(code):
                    continue
            except Exception:
                continue
        # 무장에 더해 버스트까지 지금 성립해야 '슬롯만 비면 즉시 매수'가 된다.
        if REQUIRE_CANDIDATE_BURST:
            try:
                ok_burst, _ = strat.check_burst(code)
                if not ok_burst:
                    continue
            except Exception:
                continue
        if score > best_score:
            best_code, best_score = code, score
    if best_code is None:
        return None
    return best_code, best_score


def try_slot_replacement(strat, send_telegram, replacement_count: int, now) -> int:
    """슬롯 교체 1회 시도. 반환값: 갱신된 replacement_count (교체 없었으면 그대로).

    (2026-08-03 사용자 지정) 발동 조건이 셋 다 충족될 때만 교체한다:
      ① **슬롯 만석** — 자리가 남으면 아무것도 팔지 않고 빈 칸에 사면 된다.
      ② 대체후보가 **지금 무장 중**
      ③ 대체후보의 **버스트도 지금 성립** — 즉 "슬롯만 비면 즉시 매수될 자리"
    ①이 빠져 있어서 08-03에 슬롯이 4~5칸 남는데도 3종목을 팔아 -235,860원을
    확정했다(대체후보는 끝내 매수되지 않아 자리는 그냥 비었다).
    """
    if replacement_count >= MAX_SLOT_REPLACEMENTS_PER_DAY:
        return replacement_count

    # ① 슬롯 만석일 때만. 자리가 남으면 교체의 존재 이유(슬롯 기회비용)가 0이다.
    try:
        if strat.occupied_slots() < MAX_HOLDINGS:
            return replacement_count
    except Exception:
        return replacement_count   # 슬롯 수를 못 세면 교체하지 않는다(보수적)

    # 지수 하락 가드 발동 중엔 교체하지 않는다 (2026-08-03).
    # 이 함수는 정체 종목을 **팔아서 자리만 비우고**, 실제 매수는 일반 진입
    # 경로가 한다. 그런데 가드 중엔 그 매수가 막혀 있으므로, 교체를 시도하면
    # **매도만 일어나고 대체는 안 되는** 반쪽 동작이 된다 — 손실만 확정된다.
    # 가드 사양("본전 이하는 손절선이나 14:50까지 가져간다")과도 정면 충돌한다.
    try:
        if strat._is_index_guard_active():
            return replacement_count
    except Exception:
        pass  # 가드 판정 실패는 교체를 막을 이유가 아니다(기존 동작 유지)

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
        f"| 보유 {detail['held_minutes']:.0f}분 | 순 {detail['net_rate']*100:+.2f}% "
        f"-> 대체후보 {candidate_name}({candidate_code}) 점수 {candidate_score:.1f}"
    )

    # find_stagnant_holding이 이미 분봉으로 현재가를 구해뒀으므로 재사용
    # (기존엔 여기서 count=1 분봉을 한 번 더 조회했음 — REST 중복 제거)
    current_price = detail.get("current_price") or pos.get("buy_price")

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