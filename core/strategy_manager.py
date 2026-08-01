"""
매매 전략 매니저 — 1A/Pullback/1B/1L 통합 + 하이브리드 청산 + 동적 비중 + 수수료 반영
(2026-07-27 재설계: 1S/1N/Phase2/Phase3 삭제, 슬롯 구조 개편)
(2026-07-31 재설계: 1A는 체결강도 단독 즉시진입으로 단순화, 1L은 1A와 설계
중복이라 주석처리, 1B는 WallDetector(매도벽 FSM) 제거하고 가격 하락폭
트리거+확증게이트만 남김 — 근거는 각 상수 정의부 주석 참고)

시간대 (2026-08-01 개편 — 1B 비활성화, 전략 2개 체제):
  1A       (09:00 ~ 14:50): 체결강도 100 이상 3초 유지 + 대량체결 버스트 -> 즉시매수
                            (evaluate_1a_leading_strength). 주문은 호가 두께로
                            지정가/시장가 자동 선택.
  Pullback (09:00 ~ 10:30): **눌림목자동 조건검색식 전용**. 반등확인 + 점수 + VWAP AND.
  1B       [2026-08-01 비활성화] PHASE1B_ENABLED=False — 1A와 트리거 방향이
                            정반대라 동시 적용 시 진입이 오락가락. 코드는 보존.
  1L       [2026-07-31 주석처리] 1A(체결강도 단독)와 설계 중복 — on_trade 참고

전략 라우팅 (2026-08-01, 상호배타):
  cond_name에 "눌림목자동" 포함 -> Pullback만 평가
  그 외(주도주상위/돌파자동매매용/기타) -> 1A만 평가
  한 종목이 두 전략을 오가지 않게 해서 진입 근거를 항상 하나로 고정한다.

슬롯: 1A/Pullback/1L/1B 각각 자체 상한 3개 + 전체 합산 상한 6개(MAX_HOLDINGS) 공유.
      1B는 실시간 틱 콜백에서 즉시 매수(우선권), 1A/Pullback은 조건검색 이벤트 경로.

청산:
  손절 -3%(가격) / 익절 +2.5%(순수익) / 시간정리 30분
  트레일링은 1L(주도주)에만 적용, 1A/Pullback/1B는 항상 flat 익절캡

진입 (점수 기반 — scoring.py 위임):
  score_phase1/pullback 이 하드 AND 대신 가중 점수로 통과 판정.
"""

from datetime import datetime, time, timedelta
from dataclasses import replace as _dc_replace
from typing import Optional

from .theme_manager import ThemeManager
from core.strategy.indicators import is_volume_increasing_streak, obv_momentum
from core.strategy.chemul_evaluator import ChemulState
from core.strategy.trade_flow import STRENGTH_NEUTRAL
from core.strategy.scoring import (
    ScoreConfig,
    score_phase1,
    score_pullback,
)
from core.strategy.vwap_strategy import VWAPStrategy, calc_vwap
from core.explosion_scorer import ExplosionPatternScorer
from core.strategy_performance import StrategyPerformanceTracker
from core.program_flow import ProgramFlowTracker
from core.history_fetcher import fetch_n_days_candles, to_trade_value_bins
from db import TradeRepository, WatchListRepository, SystemEventRepository
from utils.logger import logger

try:
    from api.auth import send_telegram
except Exception as e:
    logger.warning("send_telegram 사용 불가: %s", e)
    send_telegram = None


# -------------------------------------------------
# 수수료 (왕복 0.23% 고정 — 2026-07-27 확인)
# -------------------------------------------------
ROUND_TRIP_COST = 0.0023  # 왕복 수수료 및 세금


# -------------------------------------------------
# ★ Phase 설정 파일 연동 (이제 수치 수정은 phase_settings.py 에서 합니다)
# -------------------------------------------------
from config.phase_settings import COMMON, PHASE_1A, PHASE_1B
from config.phase_settings import EXIT_POLICY

# 1A/Pullback 공통 시간 윈도우 — 09:00 장 시작 즉시부터(2026-07-31, 기존 09:01에서
# 1분 앞당김, LEADING_START를 09:00으로 당긴 것과 같은 이유). 새 1A(체결강도
# 단독)는 체결 틱만 있으면 되므로 09:00부터 바로 평가 가능하고, Pullback도
# 분봉이 아직 안 쌓였으면 자연히 게이트에서 보류될 뿐 에러는 없음 — 1분을
# 그냥 버릴 이유가 없어 앞당김.
GROUP_A_START = time(9, 0)
PULLBACK_END = time(10, 30)
PHASE1A_TIGHTEN_TIME = time(10, 30)  # 이 시각부터 1A 점수 상향
PHASE1A_END = time(14, 50)
# 주도주상위 조건검색은 GROUP_A_START(09:00)부터 그대로 감시하되, 나머지
# 조건검색식(돌파자동매매용 등)은 09:20부터 감시 시작.
# (2026-07-29) on_condition_hit에서 cond_name 기준으로 적용.
OTHER_COND_START = time(9, 20)
# 즉시평가(09:00부터) 대상 조건식 이름. 주도주상위 외에 눌림목자동도 포함
# (2026-07-31, 조건식 3개 체제 재편) — Pullback 시간창은 09:00~10:30로 90분뿐이라
# 1A(~14:50)와 달리 09:20까지 19분(전체 창의 21%) 지연되면 손실이 큼. 눌림목자동은
# Pullback 전용으로 만든 검색식이라 지연시킬 이유가 없음. 돌파자동매매용은 대형주
# 위주라 상대적으로 덜 급하므로 그대로 09:20 지연 유지.
IMMEDIATE_COND_NAMES = ("주도주상위", "눌림목자동")
# 1L(주도주) 시간 윈도우 — 09:00 장 시작 즉시부터 감시(2026-07-31, 기존 09:01에서
# 1분 앞당김). 1L은 REST 분봉이 아니라 실시간 체결 틱(on_trade)만으로 판정하므로
# 09:00 정각부터도 데이터 공백 없이 그대로 동작.
LEADING_START = time(9, 0)
LEADING_END = time(10, 50)

# 진입 조건 (Phase 1A 설정값 사용)
SURGE_THRESHOLD = PHASE_1A["surge_threshold"]
MA_TOUCH_TOLERANCE = PHASE_1A["ma_tolerance"]
VOLUME_SURGE_RATIO = PHASE_1A["volume_surge_ratio"]
VOLUME_LOOKBACK = 5

# 1A 점수 커트라인 (10:30 이후 상향 + 지수경보(CAUTION) 시 추가 상향)
# (2026-07-31) 이 점수 기반 로직은 "주도주상위" 조건검색식이 아닌 경로
# (돌파자동매매용/기타)에만 계속 쓰인다 — 주도주상위 경로는 아래
# evaluate_1a_leading_strength로 대체됨(사용자 지정, 413630 실사례처럼
# HTS가 이미 거른 종목에 거래량증가지속+2분강도지속+점수를 또 씌우면
# 이중 필터링으로 지연만 유발한다는 판단).
PHASE1A_SCORE_NORMAL = 6.5
PHASE1A_SCORE_TIGHT = 8.5
PHASE1A_SCORE_CAUTION_BONUS = 1.0

# ── 1A 체결강도 단독 즉시진입 (2026-07-31 신규, 사용자 지정) ──────
# 처음엔 주도주상위 소스에만 적용했으나(HTS가 이미 강한 종목만 골라 넘겨주니
# 그 위에 거래량증가지속/테마 요구사항을 또 얹지 않는다는 취지), 같은 논리로
# 돌파자동매매용까지 확장 — 조건검색식 소스 구분 없이 1A 전체가 이제 체결강도
# 하나만 짧게(1분) 확인해서 즉시매수한다. 1L(테마리더+강도100 2분)과 사실상
# 겹친다는 점 인지: 1분<2분이라 이 경로가 1L보다 먼저 사가게 된다 — 그래서
# 1L은 이번에 통째로 주석처리(on_trade 참고). 거래량/MA/양봉 등 품질필터가
# 전부 빠지는 트레이드오프를 사용자가 인지하고 수용함(핵심 목표는 "고가 아닌
# 트리거 지점에서 매수").
PHASE1A_LEADING_STRENGTH_MIN = 100.0
# (2026-08-01 사용자 지정) 60초 -> 3초. "트리거 지점에서 사자"는 목표에 60초
# 유지는 너무 느리다는 판단. 대신 아래 대량체결 버스트 조건을 AND로 걸어서
# 짧은 창의 노이즈를 막는다 — 시간을 줄인 만큼 '체결의 질'로 보완하는 구조.
PHASE1A_LEADING_SUSTAIN_SEC = 3
# 3초 윈도우 체결강도 판정 최소 틱 수. 이 미만이면 compute_strength가 중립값
# (100.0)을 돌려주는데, 임계값도 100이라 "100 < 100 = False"로 **통과**해버린다.
# 즉 데이터가 없을수록 오히려 쉽게 뚫리는 구조였다 — 1A는 그 우연을 쓰지 않고
# 틱 수를 직접 확인해서 부족하면 명시적으로 탈락시킨다(아래 평가 함수 참고).
PHASE1A_STRENGTH_MIN_TICKS = 3
# 지수 방어 CAUTION 시 강도 임계값 배수(2026-07-31, 사용자 지정 "동적 강도
# 임계값"의 한 축) — 옛 1A 점수시스템의 PHASE1A_SCORE_CAUTION_BONUS와 같은
# 전제(시장 전체가 불안하면 컷라인을 높인다)를 강도 임계값에 적용한 것.
# 판단치 — 실전 관찰 필요.
PHASE1A_LEADING_CAUTION_MULTIPLIER = 1.2

# ── 1A 대량체결 버스트 필터 (2026-08-01 사용자 지정, 기존 60초 누적
# 거래대금 필터를 대체) ────────────────────────────────────────────
# 기존 PHASE1A_MIN_TRADE_VALUE(60초 누적 3천만원)는 잔챙이 체결이 길게 쌓여도
# 채워져서 "지금 큰 손이 때리고 있는가"를 구분하지 못했다. 3초로 창을 줄인
# 만큼, 그 3초 안에 실제로 큰 체결이 터지고 있는지를 두 갈래(OR)로 확인한다:
#   ① 단일 체결 PHASE1A_BURST_TRADE_VALUE 이상이 PHASE1A_BURST_TRADE_COUNT회 이상
#   ② 단일 체결 PHASE1A_SINGLE_TRADE_VALUE 이상이 1건이라도 (한 방에 들어온 대량)
# 둘 다 TradeFlowTracker의 같은 틱 버퍼를 재사용 — REST 호출/지연 추가 없음.
# 주의: 사용자 지시의 "3000천만"은 3천만원(30,000,000)으로 해석했다.
PHASE1A_BURST_TRADE_VALUE = 30_000_000    # 대량체결 1건의 최소 금액 (3천만원)
PHASE1A_BURST_TRADE_COUNT = 3             # 3초 안에 위 체결이 몇 건 이상이어야 하는지
PHASE1A_SINGLE_TRADE_VALUE = 100_000_000  # 단일 체결만으로 통과시키는 금액 (1억원)

# ── 1A 하이브리드 주문(지정가/시장가 자동 선택) (2026-08-01 사용자 지정) ──
# 매수 직전 매도 1~3호가에 쌓인 총 잔량 '금액'을 보고 주문 방식을 고른다:
#   잔량금액 >= PHASE1A_ASK_DEPTH_MIN  -> 호가창이 채워져 있음 -> 시장가
#       (받아줄 물량이 충분하니 즉시 체결이 유리, 슬리피지도 제한적)
#   잔량금액 <  PHASE1A_ASK_DEPTH_MIN  -> 텅 빈 호가창 -> 지정가
#       (시장가로 때리면 위쪽 호가를 훑어 올라가 크게 불리하게 체결됨)
#   호가 스냅샷 자체가 없음(판단 불가)  -> 지정가 (보수적 기본값)
PHASE1A_ASK_DEPTH_LEVELS = 3
PHASE1A_ASK_DEPTH_MIN = 50_000_000  # 5천만원

# 주도주상위 시가대비 급등 매수보류 (2026-07-31, 사용자 지정) — 개장 직후
# 시가 대비 이미 5% 이상 오른 종목은 가파른 상승 뒤 눌림(되돌림) 가능성이
# 크다고 보고 보수적으로 매수를 보류한다. 주도주상위 소스에만 적용(사용자가
# 그렇게 범위 지정) — 전일종가 기준 MAX_ENTRY_CHANGE_PCT(16%)와는 별개로,
# 이건 "당일 시가 대비"라 갭상승 여부와 무관하게 장중 상승폭만 본다.
PHASE1A_LEADING_OPEN_SURGE_CAP = 5.0

# ── 1A 개장초반 슬롯 우선순위 교체 (2026-07-31, 사용자 지정) ──────
# "장 시작하자마자 여러 종목이 동시에 조건을 만족하면 먼저 틱이 온 놈이
# 아니라 가장 강한 놈이 슬롯을 차지해야 한다"는 요구 구현. 슬롯(3개)에
# 여유가 있는 동안은 그냥 즉시매수(지연 없음) — 슬롯이 이미 꽉 찼을 때만
# 새 후보와 "가장 약한 보유종목"을 비교해서 확실히 더 강하면(마진 이상)
# 즉시 교체한다. 시간창을 개장초반(GROUP_A_START~EARLY_WINDOW_END, 09:00~
# 09:10)으로 제한한 이유: 그 이후엔 기존 slot_replacement.py(정체 10분+
# 강도/거래량 하락)가 계속 관리하므로 중복 불필요, 또 장중 내내 이 로직이
# 켜져 있으면 잘 벌고 있는 자리를 순간 강도 스파이크 하나로 뺏길 위험이 큼.
# 마진(1.2배)과 최소보유시간(30초) 가드는 슬롯을 사자마자 되파는 무의미한
# 왕복수수료 낭비를 막기 위함 — slot_replacement.py의 CANDIDATE_SCORE_MARGIN
# 관례를 그대로 재사용.
# [2026-08-01 전면 재설계 — "조건검색식 우선순위가 아니라 종목 우선순위"]
# 설계 원칙: **비교는 미리 해두고, 트리거는 즉시 실행한다.**
#   ① 슬롯에 여유가 있으면 순위를 아예 보지 않고 즉시 매수 (딜레이 0).
#      실측상 슬롯 사용률이 10%라 대부분의 시간은 이 경로다 —
#      급등 포착 속도는 예전과 완전히 동일하다.
#   ② 슬롯이 꽉 찬 순간에만 순위를 본다. 이때도 후보를 모아 기다리지 않고,
#      이미 틱 버퍼에서 계산돼 있는 값(_candidate_tier, REST 0콜/대기 0초)을
#      조회만 하므로 여전히 즉시 실행된다.
#   ③ 순위 지표는 **자기 자신 대비 비율**(거래대금 가속도 x 체결강도)이라
#      조건검색식(주도주상위=소형 급등 / 돌파자동매매용=대형주)이 달라도
#      공평하게 비교된다 — 조건식별 우선순위 자체가 필요 없어진다.
#   ④ 순위를 모르는 종목(틱 데이터 부족)은 남의 슬롯을 빼앗지만 못할 뿐,
#      빈 슬롯 매수는 그대로 한다. '모름'이 매수를 막지 않게 하는 게 중요.
PHASE1A_PRIORITY_MARGIN = 1.3       # 후보 tier >= 최약 보유종목 tier * 이 배수여야 교체
PHASE1A_PRIORITY_MIN_HOLD_SEC = 30  # 이 시간 안 된 포지션은 교체 대상에서 제외
# 교체 대상은 "아직 결과가 안 난 자리"로 제한한다 — 이미 오르고 있는 포지션을
# 순간 tier 스파이크로 빼앗으면 잘 벌고 있는 자리를 스스로 걷어차게 된다.
PHASE1A_PRIORITY_FLAT_BAND = 0.005  # 순손익 ±0.5% 이내인 포지션만 교체 대상
# tier 계산에 필요한 최소 틱 수. 미달이면 tier=0(판단 불가) -> 교체 자격 없음.
PHASE1A_TIER_MIN_TICKS = 10
PHASE1A_TIER_SHORT_SEC = 30
PHASE1A_TIER_LONG_SEC = 120

# [제안 A] 신호 세기 계층화 (2026-08-01) — 진입 트리거는 "3천만x3건 OR 단일
# 1억"을 동등하게 취급하지만, 단일 1억은 훨씬 압도적인 신호다. 슬롯이 꽉 찼을
# 때 어느 후보가 자리를 가져갈지 겨루는 tier에서는 이 차이를 반영한다.
# 배수는 상한이 고정된 이산값 — tier가 폭주하지 않도록 곱셈 항을 유한하게 묶는다.
PHASE1A_TIER_SINGLE_MULT = 1.5   # 최근 30초에 단일 1억+ 체결이 있었다
PHASE1A_TIER_BURST_MULT = 1.2    # 최근 30초에 단일 3천만+ 체결이 있었다

# [제안 C] tier 기반 매수금액 가중 (2026-08-01) — 같은 슬롯 하나를 쓰더라도
# 신호가 강한 자리에 더 태운다. **위쪽으로만** 움직인다(기존 대비 줄어드는
# 종목은 없음) — 축소 방향까지 열면 평범한 후보의 체결 수량이 0이 되는 등
# 부작용이 생기고, 지금은 자본의 2.4%만 쓰는 단계라 줄일 이유도 없다.
PHASE1A_SIZE_TIER_FULL = 1.5     # tier가 이 값 이상이면 최대 배수 적용
PHASE1A_SIZE_MAX_MULT = 1.5      # 최대 1.5배 (200만 -> 300만)

# [제안 B] 조건검색식별 성과 자동 보정 (2026-08-01) — strategy_performance의
# 축소추정 로직을 전략 축뿐 아니라 **조건검색식 축**으로도 돌린다.
# "오늘 주도주상위는 잘 되는데 돌파자동매매용은 안 된다"가 자동 반영된다.
# 우선순위(줄 세우기)가 아니라 '문턱 조정'이므로 진입 지연이 전혀 없다.
COND_PERF_PREFIX = "cond:"

# Pullback 반등 확인용 OBV(누적거래량) 확인 구간 — 몇 봉 전과 비교할지.
# (2026-07-29 신규: 거래량 없는 가짜 반등을 VWAP과 함께 걸러내는 용도)
OBV_LOOKBACK = 5

# 지수 방어 로직 임시 비활성화 스위치 (2026-07-28: 지수 급락 중 테스트 진행을 위해 OFF.
# 프로그램이 매끄럽게 동작 확인되면 True로 되돌릴 것)
MARKET_DEFENSE_ENABLED = False

# 매수 진입 등락률 상한. 이미 많이 오른 종목을 추격매수하는 걸 막기 위한 필터
# — 1A/Pullback/1B/1L 전체 전략에 동일 적용. (2026-07-28)
# 기준을 "당일 시가 대비"에서 "전일종가 대비"로 변경(2026-07-31, 실거래로 발견) —
# 시가 기준은 갭상승 출발일에 실제 상승폭을 과소평가한다. 예: 093370(후성)은
# 시가대비로는 +4.60%(통과)였지만 전일종가 대비로는 +18.18%(실제로는 상한
# 초과)였다. HTS 조건검색식(F 지표)도 전일종가 기준이라 이제 완전히 일치.
# (2026-08-01 사용자 지정) 전략별 분리. 1A는 12% -> 16%로 완화하고, 눌림목은
# 10%로 더 조인다. 근거: 1A는 "지금 매수세가 터지는 자리"를 잡는 추세추종이라
# 이미 오른 종목이 더 가는 경우를 12%에서 잘라내고 있었고, 반대로 Pullback은
# "고가에서 되돌린 자리"를 사는 전략이라 애초에 전일종가 대비 많이 오른 종목은
# 되돌림 폭도 그만큼 커서 위험하다. 같은 상한을 두 전략에 쓰면 한쪽은 기회를
# 잃고 다른 쪽은 과도한 위험을 진다.
MAX_ENTRY_CHANGE_PCT = 16.0           # 1A 및 기본
MAX_ENTRY_CHANGE_PCT_PULLBACK = 10.0  # 눌림목자동 조건검색 -> Pullback 전용

# 신규매수 전면 하드 컷오프. 1A(~14:50)/1L(~10:50)은 자체 시간 윈도우가 있지만
# 1B(FSM 감시)는 Pullback 미체결 후보를 계속 지켜보다 READY_TO_BUY가 되면 바로
# 매수해서 자체 종료 시각이 없음 — 장마감(15:30) 직전까지 실시간 틱이 들어오는
# 한 계속 매수를 시도하던 문제(2026-07-29 실전 확인, 수동 종료 전까지 지속).
# _execute_buy 단일 지점에서 전략 무관하게 막아서 1B처럼 자체 윈도우가 없는
# 전략이 추가돼도 항상 걸리게 함.
ENTRY_HARD_CUTOFF = time(15, 10)

# 지수 급락(폭락장) 대응 — MARKET_DEFENSE_ENABLED와 무관하게 항상 감시(2026-07-28).
# 코스피/코스닥 중 더 나쁜 쪽이 이 이하로 떨어지면: 트레일링 없이 전 전략
# flat SEVERE_CRASH_TAKE_PROFIT로 익절 통일(손절 -3%는 그대로), SEVERE_CRASH_ENTRY_CUTOFF
# 이후 신규매수만 중단(보유종목 청산은 그대로 진행, 강제청산 없음 — 사용자 수동판단).
# 조건검색 자체를 더 타이트하게(N봉 신고가+거래량 AND) 걸어놔서 진입 품질은
# 이미 높다는 전제.
SEVERE_CRASH_THRESHOLD = -5.0
SEVERE_CRASH_TAKE_PROFIT = 0.015
SEVERE_CRASH_ENTRY_CUTOFF = time(11, 0)

# 청산 정책 (EXIT_POLICY 딕셔너리에서 가져옴)
TAKE_PROFIT_CAP = EXIT_POLICY["default"]["take_profit_cap"]
STOP_LOSS_RATE = EXIT_POLICY["default"]["stop_loss_rate"]
TRAIL_ACTIVATE = EXIT_POLICY["default"]["trail_activate"]
TRAIL_GIVEBACK = EXIT_POLICY["default"]["trail_giveback"]
HOLDING_TIMEOUT = timedelta(minutes=EXIT_POLICY["default"]["holding_timeout_min"])

# 전략/시간대별 익절 캡 (2026-07-30 사용자 지정) — 손절(-3%)은 전부 그대로 유지.
# 1B/Pullback은 짧은 반등을 노리는 전략이라 기본 2.5%까지 기다리다 되돌림에
# 물리는 경우가 많았고, 개장 직후 10분은 변동성이 커서 빠른 확정이 유리하다는
# 판단. 익절 캡은 "매수 시점"으로 결정해 포지션 보유 중에 정책이 바뀌지 않게 한다
# (09:09에 산 종목이 09:11에 기준이 올라가면 판단이 흔들리므로).
TAKE_PROFIT_CAP_1B = 0.015
TAKE_PROFIT_CAP_PULLBACK = 0.015
TAKE_PROFIT_CAP_EARLY = 0.015
EARLY_WINDOW_END = time(9, 10)  # GROUP_A_START~이 시각 사이 매수분은 1.5%
                                # (1L도 포함 — 이 구간은 트레일링 대신 flat 1.5%)

# ── 동적 익절캡 (2026-07-30 사용자 지정) ───────────────────────
# 1.5%캡 종목이 보유 중 체결강도 상승을 보이면 캡을 2.5%로 올려 더 태우고,
# 2.5%캡 종목은 체결강도 하락 AND 거래량 하락이 동시에 오면 즉시 매도한다.
# 백테스트(07-29+07-30, 확증진입 14건, 체결강도는 분봉 대용지표로 근사):
#   익절 1.5% 고정        -> 건당 -0.659%, 승률 57.1%
#   동적 상향 + 즉시매도   -> 건당 +0.040%, 승률 64.3%  (유일한 플러스 구간)
# 파라미터 표면이 매끄러웠고(상향기준 1.0<1.2<1.5 단조, 상한캡 2.5≈3.0>2.0,
# 거래량배수 0.8이 1.0보다 우수 = '진짜 감소'를 요구해야 함), 무엇보다
# 하락 판정을 AND로 걸어야 효과가 났다(OR로는 과민해서 오히려 악화).
# 주의: 강도 임계값은 과거 틱이 없어 백테스트로 검증 불가 — 실전 관찰 필요.
# 하락 임계값은 검증된 slot_replacement와 같은 값을 재사용해 일관성 유지.
TP_CAP_UPGRADED = 0.025             # 상향 목표 캡
# 상향 판단 시점 = 순수익 +1.0% 도달 (2026-07-31 사용자 지시로 1.5%->1.0% 하향).
# 이 시점에 체결강도로 갈림길을 만든다:
#   강도 상승 -> 캡을 2.5%로 올려 더 태움
#   강도 미상승 -> 여기서 익절 확정(작은 이익을 되돌림 전에 잠금)
# 백테스트에서 이 '갈림길' 구조가 핵심이었음 — 상향기준 1.0%가 1.2/1.5%보다
# 일관되게 우수(건당 -0.18~-0.28% vs -0.4~-0.63%, 승률 64.3% vs 57.1/50.0%)했고
# 유일하게 플러스가 나온 구간이었다.
# 주의: 백테스트는 gross(가격) 기준이었으나 라이브 캡 비교는 전부 net(수수료
# 차감) 기준이라 여기서도 net으로 통일했다(0.23%p 차이는 사용자에게 유리한 방향).
TP_UPGRADE_TRIGGER = 0.010
TP_UPGRADE_STRENGTH_RATIO = 1.2     # 진입강도 대비 이 배수 이상이면 상향
TP_DECLINE_STRENGTH_RATIO = 0.8     # 진입강도 대비 이 배수 미만이면 "강도 하락"
TP_DECLINE_VOLUME_RATIO = 1.0       # volume_ratio 이 값 미만이면 "거래량 하락"
TP_VOL_CHECK_SEC = 30               # 거래량 확인(REST) 종목당 최소 간격

# ── 손실 반등 하이브리드 매도 (2026-07-31 사용자 지정) ──────────
# 손실 중인 종목이 저점에서 한 번 반등했는데 그 반등이 체결강도·거래량 어느
# 쪽으로도 뒷받침되지 않으면, 손절선(-3%)까지 끌려가지 않고 그 자리에서
# 손실을 줄여 청산한다. "수익이 나지 않더라도 손실을 최대한 적게" 대응하는 용도로,
# 되돌아온 반등 구간을 이용해 -3%보다 나은 가격에 빠져나오는 것이 목적.
# 하락 판정을 AND로 두는 건 동적 익절캡과 같은 이유 — OR은 과민해서 백테스트상
# 오히려 악화됐음(건당 -0.328% -> -0.532%, 2026-07-30).
LOSS_REBOUND_MIN = 0.01          # 저점 대비 이만큼(+1%) 이상 올라와야 '반등' 인정
LOSS_REBOUND_MIN_HOLD_MIN = 5    # 매수 후 최소 보유(분) — 직후 노이즈로 잘리는 것 방지

# ── 정체 포지션 조기 정리 (2026-08-01) ──────────────────────────
# 실측(07-28~31, 73건): 슬롯·분의 25%를 '시간정리 30분'이 먹었는데 평균
# -0.78%였고, 손익 ±0.5% 이내로 끝난 '무의미 청산' 14건이 185분(22%)을
# 점유했다. 돈을 번 청산(익절+트레일링)은 슬롯·분의 19%만 썼다.
# 30분을 다 기다리지 않고, 15분이 지나도록 아무 방향도 못 잡은 자리는
# 기회비용으로 보고 비운다. 30분 컷(HOLDING_TIMEOUT)은 최후 방어로 유지.
DEAD_POSITION_MIN = 15           # 이 시간 경과 후 판정
DEAD_POSITION_BAND = 0.005       # 순손익이 ±이 범위 안이면 '정체'로 간주

# 익절 후 재매수 상한 (2026-07-30 사용자 지정) — 손절 종목은 이미 당일 전면
# 차단이므로 사실상 익절 종목에만 적용된다. 최초 1회 + 재매수 2회 = 총 3회.
MAX_BUYS_PER_STOCK = 3

# 매도 실패 & 쿨다운 & 워밍업
MAX_SELL_FAIL = 3
REBUY_COOLDOWN = timedelta(minutes=COMMON["rebuy_cooldown_min"])
RESTART_WARMUP = timedelta(seconds=60)
BUY_WARMUP = timedelta(seconds=COMMON["buy_warmup_sec"])

# 금액 + 슬롯 (1A/Pullback/1L/1B 각각 자체 상한 3 + 전체 합산 MAX_HOLDINGS=6 공유)
POSITION_AMOUNT = COMMON["position_amount"]
MAX_HOLDINGS = COMMON["max_holdings"]
# 확장 슬롯 (2026-07-31 사용자 지정) — 평소 6개만 쓰다가, 점수가 컷라인을 크게
# 웃도는 후보가 나왔는데 슬롯교체도 성립하지 않는 상황에서만 7~8번째를 연다.
MAX_HOLDINGS_HARD = COMMON.get("max_holdings_hard", MAX_HOLDINGS)
SLOT_EXPANSION_SCORE_MARGIN = 1.5   # 컷라인 대비 이 배수 이상이어야 '정말 좋은 종목'
SLOT_EXPANSION_WAIT_SEC = 90        # 슬롯 만석이 이 시간 이상 지속돼야 확장 허용
                                    # (슬롯교체 태스크가 60초 주기 -> 한 사이클을
                                    #  넘겨도 교체가 안 됐다 = 교체 대상 없음)
PHASE1A_MAX_SLOTS = PHASE_1A["max_slots"]
# 10:30(PULLBACK_END) 이후 1A 상한 (2026-08-01 신규).
# Pullback 창은 09:00~10:30, 하루의 26%뿐인데 슬롯 3칸을 상시 예약하고 있어서
# 10:30 이후엔 그 3칸이 영구히 논다. 반면 1A는 14:50까지 도는데 캡 3에 묶여
# 있었다. 실측(07-28~31): 슬롯 용량 8,400 슬롯·분 중 실사용 834분 = 9.9%.
# 부수 효과로 **확장 슬롯(7~8)의 도달 불가 상태도 해소**된다 — 1B/1L을 끄면서
# 1A(3)+Pullback(3)=6=MAX_HOLDINGS가 되어, 보유 6 = 두 전략 모두 자기 캡이
# 꽉 찬 상태 ⟹ 7번째를 요청할 주체가 아무도 없는 구조적 데드코드였다.
PHASE1A_MAX_SLOTS_LATE = 5
PULLBACK_MAX_SLOTS = 3
PHASE1B_MAX_SLOTS = PHASE_1B["max_slots"]

MAX_WATCH_SLOTS = 10
WATCH_TIMEOUT = timedelta(minutes=10)

# 주도주 우선 진입 (주도테마 + 체결강도 100 이상 2분 지속)
LEADING_MAX_SLOTS = 3
LEADING_STRENGTH_MIN = 100.0
LEADING_SUSTAIN = timedelta(minutes=2)

# ── 1B 전면 비활성화 (2026-08-01, 사용자 지정) ─────────────────────
# "1A와 1B 동시 적용 시 애매한 부분이 있다" — 실제로 두 전략이 같은 후보 풀
# (조건검색 편입 종목)과 같은 데이터 소스(TradeFlowTracker)를 공유하면서
# 정반대 방향을 본다: 1A는 '지금 매수세가 터진다'에 사고, 1B는 '60초 내
# -2% 밀렸다'에 산다. 같은 종목이 같은 시각에 두 전략의 트리거를 번갈아
# 만족하면 진입 타이밍이 오락가락하고, 어느 로직이 그 자리를 만들었는지
# 사후 분석도 불가능해진다. 그래서 1B는 통째로 끄고 1A 하나만 남긴다.
#
# 삭제하지 않고 이 플래그 하나로 끈다 — 되살리려면 True로만 바꾸면 되고,
# 관련 코드(_try_phase1b_buy / _check_1b_confirmations / on_trade의 하락
# 트리거 / tick()의 확증 점검)는 전부 이 플래그로 가드되어 있다.
# phase1b 컨트롤러 객체 자체는 계속 살아있다 — 이제 1B 전략이 아니라
# **1A의 체결틱/호가 데이터 파이프라인**으로 쓰이기 때문(TradeFlowTracker,
# OrderbookTracker). 이 객체를 없애면 1A가 눈이 먼다.
PHASE1B_ENABLED = False

# ── 1B 진입 트리거 재설계 (2026-07-31, 사용자 지정 "수술") ──────────
# 기존엔 WallDetector(호가 1~2단 매도잔량 감시)의 5단계 FSM(눌림→벽등장→
# 벽축소+강도상승→벽소실)을 거쳐야 확증게이트로 넘어갔는데, 이 FSM을 통째로
# 제거하고 "가격이 60초 내 PHASE1B_PULLBACK_PCT 이상 하락"만으로 바로
# 확증게이트(아래 PHASE1B_CONFIRM_WAIT)로 직행한다. 근거:
#   ① WallDetector 파라미터(detect_multiplier=5.0/shrink_ratio=0.7/
#      disappear_ratio=0.2)는 코드 도입 시점부터 "TBD: 실데이터로 튜닝"이라고
#      명시된 placeholder였고, 이후 한 번도 튜닝된 적이 없었다.
#   ② 호가 잔량 이력은 어느 데이터 공급자(대신증권 포함)로도 영구 조회 불가
#      (거래소가 소매용으로 아카이브를 안 함) — 즉 이 필터는 원리상 백테스트
#      검증이 영원히 불가능하다.
#   ③ FSM 자체가 지연의 큰 축이었다(413630 실사례: 확증게이트 전 FSM 단계에서만
#      19분 소요, 총 지연 20분 중 대부분).
#   ④ 반면 확증게이트(가격/캔들 데이터만 사용)는 07-30 백테스트로 이미 검증됨
#      (진입 37→14건, 승률 29.7%→50.0%) — 그 검증된 부분만 남기고 검증 불가능한
#      부분(WallDetector)을 걷어낸 것.
# PHASE1B_PULLBACK_PCT 재조정 근거: 기존 -1.5%는 같은 이유로 미검증 placeholder
# 였음. 오늘(07-31) 실데이터로 -1.0/-1.5/-2.0/-2.5/-3.0% 5개 후보를 확증게이트와
# 묶어 재생한 결과 하락폭이 깊을수록 단조롭게 개선됐다:
#   -1.0%: 35건 승률54.3% 평균-0.030% | -1.5%(기존): 19건 승률57.9% 평균-0.220%
#   -2.0%: 9건 승률77.8% 평균+1.261% | -2.5%: 4건 승률100% 평균+2.145%(표본 작음)
# -2.0%를 채택 — 표본이 -2.5%/-3.0%보다 커서(9건) 상대적으로 더 신뢰할 만하고,
# 단조 개선 추세의 중간 지점이라 표본이 작은 극단값에 과최적화됐을 위험이
# -2.5% 이상보다 낮다고 판단. **표본이 하루치(35종목)뿐이라는 한계는 명확히
# 인지할 것** — 내일 이후 실거래로 반드시 재검증.
PHASE1B_PULLBACK_PCT = -2.0
PHASE1B_PULLBACK_WINDOW_SEC = 60

# ── 1B 반등확증 (2026-07-30 신규) ──────────────────────────────
# 1B FSM은 "60초 내 -1.5% 하락"을 진입 요건으로 요구하는 역추세 전략인데,
# 매도벽 소실 '즉시' 매수해서 하락 중 반복 진입(칼날잡기)이 발생했음.
# 07-29+07-30 실거래 37건의 진입 후 분봉 경로를 재생해 확증규칙 10종을
# 백테스트한 결과, "신호봉 고가를 1분봉 종가로 돌파"가 가장 강건했음:
#   진입 37 -> 14건, 건당 평균 -1.33% -> -0.33%, 승률 29.7% -> 50.0%
#   (07-29 -1.44%->-0.27%, 07-30 -0.83%->-0.48% — 양일 모두 개선)
# 손절/익절 표면도 매끄러운 단조 형태로 과최적화 징후가 없었고, 같은 봉에서
# 손절/익절 동시 도달 시 손절 우선으로 계산한 보수적 가정이었음.
# 경제적 의미: "떨어진 자리를 회복해야 산다" — 반등을 가격으로 확인.
PHASE1B_CONFIRM_WAIT = timedelta(minutes=5)   # 이 시간 내 미돌파면 매수 포기
PHASE1B_CONFIRM_CHECK_SEC = 55                # 1분봉 종가 기준이라 분당 1회만 확인(REST 절약)

# ── 1B 확증 슬리피지 축소 (2026-07-31) ──────────────────────────
# 07-31 실거래 재현: 확증 성립 5건의 실제 매수가가 기준선(ref_high) 대비
# +0.15%~+1.72% 더 비쌌고, 대기시간은 57~125초였음. 원인은 확증 '규칙'
# 자체(완성봉 종가 돌파)가 아니라, REST 완성봉 폴링을 55초 간격으로만 하는
# '감지 지연'이었음 — 확증 규칙(R2)은 07-30 백테스트로 검증된 값이라 그대로
# 유지하고, 감지 지연만 없앤다. 무료인 실시간 체결가(TradeFlowTracker,
# 이미 FSM이 켜둔 tick 구독으로 공짜로 들어옴)로 먼저 기준선 돌파 여부를
# 확인해서, 아직 안 넘었으면 REST 호출 자체를 생략(429 예산도 기존보다 아낌
# — 기존엔 넘었든 안 넘었든 55초마다 무조건 폴링). 넘었을 때만 아래 값
# 간격으로 REST를 조회해 '완성봉 종가' 확정 여부를 검증 — 확증의 질(완성봉
# 종가 기준)은 그대로, 알아채는 속도만 tick() 주기(10초) 수준으로 빨라짐.
PHASE1B_CONFIRM_TICK_PRECHECK = True
PHASE1B_CONFIRM_REST_GAP_SEC = 12             # 기준선 돌파 후 REST 재확인 최소 간격

# MDD 일손실 차단
DAILY_LOSS_LIMIT = COMMON["mdd_daily_loss_limit"]


def _notify(msg: str, target: str = "signal"):
    if send_telegram is None:
        return
    try:
        send_telegram(msg, target=target)
    except Exception as e:
        logger.warning("텔레그램 전송 실패: %s", e)


class StrategyManager:
    def __init__(
        self,
        kiwoom_rest,
        order_manager,
        phase1b_controller=None,
        portfolio_optimizer=None,
        now_func=None,
    ):
        self.api = kiwoom_rest
        self.order_manager = order_manager
        self.phase1b = phase1b_controller
        self.optimizer = portfolio_optimizer
        self._now = now_func or datetime.now

        self.holdings: dict[str, dict] = {}
        # 재평가 대상 후보 (watchlist_reentry / slot_replacement가 순회)
        self.watch_list_today: set[str] = set()
        # DB(watch_list_log)에 실제로 행을 쓴 종목 (2026-08-01 분리).
        # 기존엔 watch_list_today 하나로 두 역할을 겸해서, 평가 지연 경로가
        # watch_list_today에 먼저 넣어버리면 나중에 _record_watch_list가
        # "이미 있음"으로 조기 반환해 **DB 행이 영영 안 남는** 구멍이 있었다
        # (09:20 지연 종목/장 시작 전 편입 종목 → 일일 백테스트·틱아카이브
        #  유니버스에서 통째로 누락).
        self._watch_db_written: set[str] = set()
        self.pending: set[str] = set()
        self._stock_names: dict[str, str] = {}
        self._cond_names: dict[str, str] = {}  # stock_code -> 최초 편입 조건검색식 이름
        self._opening_prices: dict[str, float] = {}  # stock_code -> 당일 시가 (1A 서지율 점수용)
        self._prev_closes: dict[str, float] = {}  # stock_code -> 전일종가 (등락률 상한 체크용, 2026-07-31)
        # 평상시 상한(MAX_HOLDINGS)이 꽉 찬 시각 — 확장 슬롯(7~8) 판정용 (2026-07-31)
        self._soft_cap_full_since: Optional[datetime] = None
        # 장중 전략 성과 추적 — 잘 되는 전략의 컷라인을 낮추고 안 되는 전략은
        # 물러나게 하는 자동 우선순위 조정 (2026-07-31, core/strategy_performance.py)
        self.perf = StrategyPerformanceTracker(now_func=self._now)
        # 프로그램 매매 유입 관측 (2026-07-31) — 지금은 기록/백테스트 전용이고
        # 매매 판단에는 전혀 쓰지 않는다. 데이터 소스가 확정되면(WS 0B FID 또는
        # 시장 전체 랭킹 REST) record_minute()으로 넣어주기만 하면 된다.
        self.program_flow = ProgramFlowTracker(now_func=self._now)
        self._watch_scores: dict[str, float] = (
            {}
        )  # stock_code -> 워치리스트 등재 시 점수 (슬롯교체용, 2026-07-26)
        self.vwap_strategy = VWAPStrategy()  # 눌림목(1A) VWAP AND 필터
        self.explosion_scorer = (
            ExplosionPatternScorer()
        )  # 거래대금 폭발 이력 스코어러 (2026-07-26)

        # 주도테마 초기화 (등락률 기반, 2026-07-06 재설계)
        self.theme_mgr = ThemeManager(rest_api=kiwoom_rest)
        self.theme_mgr.fetch_themes_from_github()
        self.theme_mgr.start_auto_update()
        # 1L(주도주) 체결강도 100 이상 지속시간 추적 {stock_code: 최초 감지 시각}
        self._leading_since: dict[str, datetime] = {}

        # 1L 진단용 상태 (2026-07-30) — 1L이 연일 0건인데 on_trade는 틱마다
        # 호출되는 핫패스라 매 틱 로깅이 불가능해서, 상태 전이(타이머 시작/리셋/
        # 지속완료)와 주기 요약만 남긴다. 카운터는 여러 워커 스레드에서 동시
        # 증가할 수 있어 정확한 값이 아닐 수 있음(진단용이므로 근사치 허용,
        # 락을 걸면 틱 처리 경로가 느려짐).
        self._l1_diag = {
            "ticks": 0,        # 1L 판정까지 도달한 틱 수
            "theme_ok": 0,     # 주도테마 소속이었던 틱
            "strength_ok": 0,  # 체결강도 >= LEADING_STRENGTH_MIN 이었던 틱
            "window_ok": 0,    # 시간창(09:00~10:50) 안이었던 틱
            "both_ok": 0,      # 3조건 모두 충족(=타이머 유지)이었던 틱
        }
        self._l1_diag_last_report = self._now()
        self._l1_max_sustain_sec = 0.0          # 오늘 도달한 최장 연속 유지 시간
        self._l1_reset_logged_at: dict[str, datetime] = {}  # 리셋 로그 throttle
        self._l1_block_logged_at: dict[str, datetime] = {}  # 차단 로그 throttle

        # 1B 반등확증 대기 {code: {ref_high, since, last_check}} (2026-07-30)
        self._1b_confirm: dict[str, dict] = {}

        # ── 진입 진단 (2026-08-01 신규) ────────────────────────────
        # "조건검색엔 계속 포착되는데 매수가 안 된다"를 장중에 원인별로
        # 판단할 수 있게 하는 관측 인프라. 종목별 마지막 탈락 사유와
        # 사유별 종목 수를 들고 있다가 주기적으로 텔레그램으로 요약한다.
        # 로그를 뒤지지 않고도 "필터가 막는 중"인지 "코드가 고장난 것"인지
        # 구분하는 게 목적이라, 카테고리를 그 두 축으로 나눠 집계한다.
        self._last_reject: dict[str, tuple[str, str, datetime]] = {}  # code -> (분류, 원문, 시각)
        self._last_buy_at: Optional[datetime] = None

        # 매수 진행중(pending) 종목의 전략 — 슬롯 오버부킹 방지용 (2026-08-01).
        # _execute_buy가 주문 직전에 넣고 finally에서 지운다.
        self._pending_strategy: dict[str, str] = {}

        # 종목별 당일 매수 횟수(익절 후 재매수 상한용) + 동적캡 거래량확인 throttle
        self._buy_count_today: dict[str, int] = {}
        self._tp_vol_checked_at: dict[str, datetime] = {}

        self.sell_fail_count: dict[str, int] = {}
        self.sell_blocked: set[str] = set()
        self.sold_at: dict[str, datetime] = {}
        self._stoploss_blocked: set[str] = set()  # 손절로 나간 종목(당일 재매수 금지)
        self._buy_success_count = 0
        # MDD 일손실 차단 (실현손익 기준 -3%)
        self._base_capital = None  # 기준자본 (첫 매수 시도 때 1회 기록)
        self._daily_realized = 0.0  # 오늘 실현손익 누적
        self._risk_tripped = False  # 차단기 발동 여부
        self._risk_date = self._now().date()
        # 지수 방어 (코스피+코스닥 중 더 나쁜 쪽 기준 threshold 조절/매수 차단)
        self._kospi_rate = 0.0
        self._kosdaq_rate = 0.0
        self._market_rate_at = None  # 마지막 조회 시각
        # WS 재연결 격리기간 (신규매수만 보류, 청산감시는 계속) — main.py가 설정
        self.quarantine_until = self._now()
        self._last_market_mode = "NORMAL"  # 지수방어 모드 전환 알림용
        self._last_severe_crash_state = False  # 지수 급락 대응 모드 전환 알림용

        # 점수 기반 진입 설정 (1A 전용). surge_min은 score_phase1이 안 쓰는
        # 필드라 ScoreConfig 기본값 그대로 둠.
        self.score_cfg = ScoreConfig(
            surge_target=SURGE_THRESHOLD,
            ma_tolerance=MA_TOUCH_TOLERANCE,
            volume_target=VOLUME_SURGE_RATIO,
            threshold_ratio=0.75,
        )
        # Pullback 전용 cfg (2026-07-29 분리) — MA/양봉은 게이트(눌림성립+반등확인)로
        # 이미 확정된 값이라 점수에서 빼고 거래량/강도/OBV로 9점 재분배, 컷라인도
        # 낮춤(0.75->0.5). 실측 근거: 1A와 같은 cfg를 쓰던 기존엔 MA+양봉이 항상
        # 만점(4/9 고정)이고 강도는 phase1b 감시 시작 전이라 거의 항상 0점이라,
        # 오늘 점수단계 277건이 전부 탈락(0건 통과)했었음.
        self.pullback_score_cfg = ScoreConfig(
            ma_tolerance=MA_TOUCH_TOLERANCE,
            volume_target=VOLUME_SURGE_RATIO,
            w_volume=4.0,
            w_strength=3.0,
            w_obv=2.0,
            threshold_ratio=0.5,
        )
        self._restore_from_db()
        self._last_phase = self.get_current_phase()

    # ========================================
    # 당일/전일 분봉 병합 (장초반 MA 계산용)
    # ========================================
    def _get_merged_candles(
        self, stock_code: str, interval: int = 1, count: int = 60
    ) -> list:
        """
        당일 분봉만으로 개수가 부족할 경우, 전일 분봉을 끌어다 붙여서 개수를 맞춤.
        장 초반(예: 9:20)에 30MA 계산을 위해 30개를 요청해도 20개만 올 때 유용.
        """
        # 1. 일단 당일 데이터 요청
        today_candles = self.api.get_minute_candles(
            stock_code, interval=interval, count=count
        )

        # 2. 요구 개수를 채웠거나 당일 데이터가 아예 없으면 그대로 반환
        if not today_candles or len(today_candles) >= count:
            return today_candles or []

        # 3. 부족한 개수 계산
        needed = count - len(today_candles)

        # 4. 전일 날짜 계산
        yesterday = (self._now() - timedelta(days=1)).strftime("%Y%m%d")

        try:
            # 5. 전일 데이터 요청 (최근 봉부터 과거로 내려옴)
            yesterday_candles = self.api.get_minute_candles(
                stock_code, interval=interval, count=needed, base_date=yesterday
            )

            if yesterday_candles:
                # 6. 전일 데이터(과거) + 당일 데이터(최근) 순서로 병합
                merged = yesterday_candles + today_candles
                logger.info(
                    f"📊 [{stock_code}] 분봉 부족 보완: 당일 {len(today_candles)}개 "
                    f"+ 전일 {len(yesterday_candles)}개 = 총 {len(merged)}개"
                )
                return merged
        except Exception as e:
            logger.warning(f"[{stock_code}] 전일 분봉 조회 실패: {e}")

        # 전일 조회 실패 시 그냥 당일 데이터라도 반환
        return today_candles

    @staticmethod
    def _entry_change_cap(sub_strategy: str, cond_name: str) -> float:
        """전략별 매수 등락률 상한(전일종가 대비 %) (2026-08-01 사용자 지정).

        눌림목 = 10% / 그 외(1A 등) = 16%.
        sub_strategy와 cond_name **둘 다** 확인한다 — 라우팅상 둘은 항상 같이
        움직이지만(눌림목자동 -> 1A_눌림), 어느 한쪽만 보면 나중에 경로가
        추가됐을 때 조용히 느슨한 상한이 적용될 수 있다. 더 보수적인 쪽으로
        수렴시키는 게 안전하다.
        """
        if sub_strategy == "1A_눌림" or "눌림목자동" in (cond_name or ""):
            return MAX_ENTRY_CHANGE_PCT_PULLBACK
        return MAX_ENTRY_CHANGE_PCT

    @staticmethod
    def cond_perf_key(cond_name: str) -> str:
        """조건검색식 성과 추적용 정규화 키 (2026-08-01, 제안 B).

        cond_name은 "주도주상위+돌파자동매매용"처럼 병합돼 들어올 수 있는데,
        그대로 키로 쓰면 조합마다 표본이 쪼개져 최소표본(3건)에 영원히 도달하지
        못한다. 우선순위가 아니라 **표본을 모으기 위한** 대표값 하나로 접는다.
        (주도주상위가 1A의 주 소스이므로 먼저 확인)
        """
        cn = cond_name or ""
        for name in ("주도주상위", "돌파자동매매용", "눌림목자동"):
            if name in cn:
                return COND_PERF_PREFIX + name
        return COND_PERF_PREFIX + "기타"

    @staticmethod
    def _score_ratio(info: dict) -> float:
        """전략 공통 비교용 '컷라인 대비 비율' (2026-08-01 신규).

        1A는 체결강도(0~300), Pullback은 점수(0~9)로 스케일이 완전히 달라서
        원점수를 그대로 비교하면 슬롯교체·확장슬롯 판정이 전부 1A 쪽으로
        기울었다. 1.0 = 자기 컷라인을 정확히 충족, 1.5 = 컷라인의 1.5배.
        점수 정보가 없는 경로(1B/1L)는 0.0 — 비교 대상에서 자연히 빠진다.
        """
        score = info.get("score")
        thr = info.get("score_threshold")
        if score is None:
            return 0.0
        try:
            score = float(score)
            thr = float(thr) if thr else 0.0
        except (TypeError, ValueError):
            return 0.0
        if thr <= 0:
            return 0.0
        return score / thr

    def _today_open(self, candles: list) -> float:
        """분봉 리스트에서 **당일** 첫 봉의 시가를 뽑는다 (2026-08-01 신규).

        기존 호출부들은 `candles[-1]["open"]`을 '당일 시가'로 썼는데, 이
        프로젝트의 분봉은 내림차순(최신->과거)이라 그건 '가장 오래된 봉'이고,
        개장 직후엔 키움이 요청 개수를 채우려고 **전일 봉까지** 붙여 주므로
        실제로는 전일 데이터였다(07-30 09:01 실측: candles[-1] = 07-29 15:09봉).
        그 결과 1A의 "시가대비 +5% 매수보류"가 사실상 '전일종가 대비 +5%'로
        동작해 개장 초반 주도주 대부분이 잘렸다.

        time_str로 오늘 봉만 걸러 가장 이른 봉의 시가를 쓴다. 오늘 봉이
        하나도 없으면 0.0 — 호출부는 0이면 시가 기반 필터를 건너뛴다
        (모르는 값으로 막지 않는다).
        """
        if not candles:
            return 0.0
        today_str = self._now().strftime("%Y%m%d")
        today_only = [
            c for c in candles
            if str(c.get("time_str", "")).startswith(today_str)
        ]
        if not today_only:
            return 0.0
        first = min(today_only, key=lambda c: str(c.get("time_str", "")))
        try:
            return float(first.get("open") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ========================================
    # 순수익률 계산 (수수료 차감)
    # ========================================
    @staticmethod
    def _gross_rate(buy_price: float, current_price: float) -> float:
        return (current_price - buy_price) / buy_price if buy_price else 0.0

    @staticmethod
    def _net_rate(buy_price: float, current_price: float) -> float:
        """수수료(왕복)+세금 차감 순수익률."""
        if not buy_price:
            return 0.0
        gross = (current_price - buy_price) / buy_price
        return gross - ROUND_TRIP_COST

    @staticmethod
    def _net_profit(buy_price: float, current_price: float, qty: int) -> float:
        """실제 순손익 금액 (왕복 수수료/세금 차감, _net_rate와 동일 기준)."""
        buy_amt = buy_price * qty
        gross_profit = (current_price - buy_price) * qty
        return gross_profit - buy_amt * ROUND_TRIP_COST

    # ========================================
    # 상태 복원
    # ========================================
    def _restore_from_db(self):
        warmup_until = self._now() + RESTART_WARMUP
        for h in TradeRepository.find_holdings():
            buy_price = float(h["buy_price"])
            self.holdings[h["stock_code"]] = {
                "trade_id": h["id"],
                "buy_price": buy_price,
                "buy_quantity": int(h["buy_quantity"]),
                "buy_time": h["buy_time"],
                "stock_name": h["stock_name"],
                "strategy_phase": h["strategy_phase"],
                "sub_strategy": h.get("sub_strategy"),
                "highest_price": buy_price,
                "ma20": None,  # 20MA 캐시 (미계산)
                "ma20_updated": None,  # 20MA 갱신 시각
                "warmup_until": warmup_until,
            }
        for w in WatchListRepository.find_by_date(self._now().date()):
            self.watch_list_today.add(w["stock_code"])
            self._watch_db_written.add(w["stock_code"])

        blocked, cooldowns, counts = self._restore_daily_risk_state()

        logger.info(
            "DB 복원: 보유 %d (1A=%d, 눌림=%d, 1B=%d, 1L=%d) / 워치 %d / 워밍업 %ds "
            "/ 손절차단 %d종목 / 쿨다운 %d종목 / 매수횟수기록 %d종목",
            len(self.holdings),
            self.count_holdings_by_strategy("1A"),
            self.count_holdings_by_strategy("1A_눌림"),
            self.count_holdings_by_strategy("1B"),
            self.count_holdings_by_strategy("1L"),
            len(self.watch_list_today),
            int(RESTART_WARMUP.total_seconds()),
            blocked, cooldowns, counts,
        )

    def _restore_daily_risk_state(self) -> tuple[int, int, int]:
        """당일 리스크 상태(손절차단/쿨다운/재매수횟수) DB 재구성 (2026-07-31).

        _stoploss_blocked/sold_at/_buy_count_today는 전부 메모리 전용 set/dict라
        재시작하면 통째로 비워졌다 — 그날 이미 손절한 종목이 재시작 직후 재매수
        차단 없이 그대로 다시 매수되는 실거래 사고로 발견됨(413630 씨피시스템,
        09:53 손실청산 -> 재시작 -> 10:09 재매수). holdings처럼 여기도 DB에서
        재구성해야 재시작이 리스크 관리 기록을 지우는 구멍이 없어진다.

        기준: 오늘자 trades 전체(보유+청산)를 한 번에 읽어
          - 매수 횟수: 행 하나당 1회(재매수 상한 카운트)
          - 손절 차단: status='closed' AND 순손익(profit_amount, 없으면
            profit_rate로 폴백) < 0 인 종목
          - 쿨다운: status='closed'인 행의 sell_time 중 가장 최근 값
        반환: (손절차단 종목수, 쿨다운 대상 종목수, 매수횟수 기록 종목수) — 로그용."""
        try:
            rows = TradeRepository.find_by_date(self._now().date())
        except Exception as e:
            logger.warning("당일 리스크 상태 DB 복원 실패(빈 상태로 시작): %s", e)
            return 0, 0, 0

        for r in rows:
            code = r.get("stock_code")
            if not code:
                continue
            self._buy_count_today[code] = self._buy_count_today.get(code, 0) + 1

            if r.get("status") != "closed":
                continue
            sell_time = r.get("sell_time")
            if sell_time and (code not in self.sold_at or sell_time > self.sold_at[code]):
                self.sold_at[code] = sell_time

            net = r.get("profit_amount")
            if net is None:
                pr = r.get("profit_rate")
                net = -1 if (pr is not None and float(pr) < 0) else 0
            if net is not None and float(net) < 0:
                self._stoploss_blocked.add(code)

        return len(self._stoploss_blocked), len(self.sold_at), len(self._buy_count_today)

    # ========================================
    # 주기 루프 (주기 호출)
    # ========================================
    def tick(self):
        now = self._now()

        # 지수 방어막 HALT면 루프 전체 정지
        if self._get_market_defense_mode() == "HALT":
            return

        cur_phase = self.get_current_phase()
        if cur_phase != self._last_phase:
            self._last_phase = cur_phase

        # 감시 종목 정리 (2026-08-01 전면 수정 — 치명적 회귀 해소)
        #
        # 기존엔 "10:30(PULLBACK_END) 이후 미보유 감시종목 전부 stop_watching"
        # 이었다. 이건 1B(10:30 종료)용 정리였는데, 1A가 같은 phase1b의
        # TradeFlowTracker를 쓰도록 바뀌면서 **1A의 체결틱 버퍼를 10초마다
        # 통째로 지우는** 구조가 됐다(stop_watching -> trade_flow.reset).
        # 1A 시간창은 14:50까지인데 10:30 이후 버퍼가 계속 비워지니
        # is_intensity_sustained가 항상 실패 -> 1A 창의 70%가 죽어 있었다
        # (재현 검증: 동일 틱 흐름에서 3초 거래대금이 1/6로 떨어짐).
        #
        # 새 규칙: 1A 창(14:50)까지는 후보/보유 종목의 감시를 유지하고,
        # '더 이상 후보도 보유도 아닌' 종목만 정리해서 메모리를 묶는다.
        self._cleanup_watched(now)

        self._track_soft_cap_full(now)
        # 1B 비활성화(2026-08-01) — PHASE1B_ENABLED로 가드. 코드는 보존.
        if PHASE1B_ENABLED:
            self._check_1b_confirmations()
        self._update_dynamic_caps()
        self.check_timeouts()

    def _cleanup_watched(self, now):
        """phase1b 감시 목록 정리 (2026-08-01 신규).

        감시 목록은 1A의 데이터 수집 대상이다(체결틱 + 호가). 따라서
          - 보유 종목: 항상 유지 (동적 익절캡/손실반등 매도가 실시간 강도를 봄)
          - 오늘 후보(watch_list_today): 1A 창(PHASE1A_END)까지 유지
          - 그 외: 즉시 해제
        1A 창이 끝나면 후보도 해제해서 메모리를 정리한다.
        """
        if not self.phase1b:
            return
        in_window = now.time() < PHASE1A_END
        for code in list(self.phase1b.watched):
            if code in self.holdings:
                continue
            if in_window and code in self.watch_list_today:
                continue
            self.phase1b.stop_watching(code)

    # ========================================
    # Phase 판별 (09:00~14:50 그룹A 활성 여부)
    # ========================================
    def get_current_phase(self) -> Optional[int]:
        t = self._now().time()
        if GROUP_A_START <= t < PHASE1A_END:
            return 1
        return None

    def _ensure_base_capital(self):
        """기준자본 1회 기록 (주문가능 + 보유 매입원가). 재시작 시에도 근사 유지."""
        if self._base_capital is not None:
            return
        try:
            deposit = float(self.api.get_orderable_amount())
        except Exception:
            return
        holding_cost = sum(
            p["buy_price"] * p["buy_quantity"] for p in self.holdings.values()
        )
        self._base_capital = deposit + holding_cost
        logger.info(
            "MDD 기준자본 기록: %s원 (주문가능 %s + 보유원가 %s)",
            f"{self._base_capital:,.0f}",
            f"{deposit:,.0f}",
            f"{holding_cost:,.0f}",
        )

    def _risk_daily_reset(self):
        today = self._now().date()
        if today != self._risk_date:
            self._risk_date = today
            self._daily_realized = 0.0
            self._risk_tripped = False
            self._base_capital = None
            self._buy_count_today.clear()  # 재매수 상한도 하루 단위 (2026-07-30)

    def risk_can_trade(self) -> bool:
        """일손실 -3% 차단기. 트립되면 신규 매수 전면 금지(청산은 계속 작동)."""
        self._risk_daily_reset()
        self._ensure_base_capital()
        if self._risk_tripped:
            return False
        if self._base_capital and self._base_capital > 0:
            loss_rate = self._daily_realized / self._base_capital
            if loss_rate <= DAILY_LOSS_LIMIT:
                self._risk_tripped = True
                logger.warning(
                    "MDD 일손실 차단 발동: 실현 %s원 (%.2f%%) <= 한도 %.1f%%",
                    f"{self._daily_realized:,.0f}",
                    loss_rate * 100,
                    DAILY_LOSS_LIMIT * 100,
                )
                _notify(
                    f"🛑 MDD 일손실 차단 발동\n"
                    f"실현손익: {self._daily_realized:,.0f}원 ({loss_rate*100:.2f}%)\n"
                    f"기준자본 {self._base_capital:,.0f}원 대비 한도 {DAILY_LOSS_LIMIT*100:.1f}% 초과\n"
                    f"→ 오늘 신규 매수 전면 차단 (보유분 청산은 계속)"
                )
                return False
        return True

    def _refresh_market_rates(self):
        """코스피/코스닥 등락률 1분 캐시. 실패해도 기존값 유지(봇 안 멈춤)."""
        now = self._now()
        if (
            self._market_rate_at is not None
            and (now - self._market_rate_at).total_seconds() < 60
        ):
            return
        try:
            self._kospi_rate = self.api.get_index_change_rate("001")
        except Exception:
            pass  # 조회 실패 시 기존 캐시값 유지
        try:
            self._kosdaq_rate = self.api.get_index_change_rate("101")
        except Exception:
            pass
        self._market_rate_at = now

    def _adjusted_cfg(self, base_cfg):
        """지수 방어 모드(코스피/코스닥 중 나쁜 쪽) 반영해 threshold_ratio 조절한 cfg 복사본 반환.
        CAUTION/HALT 모두 타이트하게(HALT는 can_buy_more에서 이미 매수 차단되지만 이중 방어)."""
        mode = self._get_market_defense_mode()
        if mode == "NORMAL":
            return base_cfg
        new_ratio = max(0.5, min(1.0, base_cfg.threshold_ratio + 0.05))
        return _dc_replace(base_cfg, threshold_ratio=new_ratio)

    def can_buy_more(self, info: dict | None = None, sub_strategy: str | None = None) -> bool:
        if not self.risk_can_trade():
            return False
        if self._get_market_defense_mode() == "HALT":
            return False
        if self._now() < self.quarantine_until:
            return False
        held = self.occupied_slots()
        # 오늘 성과가 나쁜(COLD) 전략은 공유 슬롯 마지막 칸을 다른 전략에
        # 양보한다 — 잘 안 되는 전략이 마지막 자리를 선점해 버리는 것을 막는
        # 우선순위 장치. (2026-07-31)
        soft_limit = MAX_HOLDINGS
        if sub_strategy:
            soft_limit = self.perf.shared_slot_limit(sub_strategy, MAX_HOLDINGS)
        if held < soft_limit:
            return True
        # 평상시 상한(6)을 넘어선 확장 슬롯(7~8)은 예외 경로 — info가 있고
        # 그 점수가 컷라인을 크게 웃도는 후보에게만 열린다. (2026-07-31)
        if held >= MAX_HOLDINGS_HARD:
            return False
        if held < MAX_HOLDINGS:
            return False  # COLD 양보로 막힌 칸은 확장 경로로 우회할 수 없음
        return self._can_use_expansion_slot(info, sub_strategy)

    def _can_use_expansion_slot(
        self, info: dict | None, sub_strategy: str | None = None
    ) -> bool:
        """확장 슬롯(7~8번째) 사용 자격 판정 (2026-07-31 사용자 지정).

        "평소 6개만 쓰되, 정말 좋은 종목이 포착됐는데 슬롯교체가 애매하면
        남은 슬롯을 쓴다"는 요구를 세 조건으로 구현한다:
          ① 점수가 자기 컷라인의 SLOT_EXPANSION_SCORE_MARGIN배 이상 — 전략마다
             만점 스케일이 달라(1A 12점 / Pullback 7점) 절대값 대신 컷라인 대비
             비율로 판정해야 전략 간 형평이 맞는다.
          ② 슬롯이 꽉 찬 상태가 SLOT_EXPANSION_WAIT_SEC 이상 지속 — 슬롯교체
             태스크가 60초 주기로 돌므로, 이 시간을 넘겼다는 건 교체할 만한
             정체 종목이 없었다는 뜻("슬롯교체가 애매한 경우")이다.
          ③ 점수 정보가 없는 경로(1B/1L 등 실시간 틱 진입)는 확장 대상 아님 —
             비교 기준이 없어 '정말 좋은 종목'을 판정할 수 없기 때문.
        """
        if not info:
            return False
        score = info.get("score")
        thr = info.get("score_threshold")
        if not score or not thr or thr <= 0:
            return False
        # 오늘 성과가 좋은(HOT) 전략은 마진을 완화해 확장 슬롯을 더 쉽게 쓴다
        # (2026-07-31) — 우선순위를 슬롯 개수가 아니라 '문턱'으로 주는 방식.
        margin = SLOT_EXPANSION_SCORE_MARGIN
        if sub_strategy:
            margin = self.perf.expansion_margin(sub_strategy, margin)
        if score < thr * margin:
            return False
        since = self._soft_cap_full_since
        if since is None:
            return False
        if (self._now() - since).total_seconds() < SLOT_EXPANSION_WAIT_SEC:
            return False
        logger.info(
            "확장 슬롯 사용 (보유 %d/%d -> 상한 %d, 전략 %s/%s): 점수 %.1f >= "
            "컷라인 %.1f x%.2f, 슬롯 만석 %.0f초 지속(교체 대상 없음)",
            len(self.holdings), MAX_HOLDINGS, MAX_HOLDINGS_HARD,
            sub_strategy or "?", self.perf.tier(sub_strategy) if sub_strategy else "-",
            score, thr, margin,
            (self._now() - since).total_seconds(),
        )
        return True

    def may_expand_slots(self) -> bool:
        """확장 슬롯이 열려있을 가능성이 있는 상태인지 (점수 무관, 값싼 사전확인).
        watchlist_reentry처럼 '슬롯 여유 없으면 REST 호출도 안 함'으로 조기
        반환하는 경로가, 확장 가능한 상황까지 싸잡아 막아버리지 않도록 하는 용도.
        실제 허용 여부는 후보별 점수를 보는 _can_use_expansion_slot이 결정한다."""
        held = self.occupied_slots()
        if held < MAX_HOLDINGS or held >= MAX_HOLDINGS_HARD:
            return False
        since = self._soft_cap_full_since
        if since is None:
            return False
        return (self._now() - since).total_seconds() >= SLOT_EXPANSION_WAIT_SEC

    def _track_soft_cap_full(self, now):
        """평상시 상한(6개)이 언제부터 꽉 차 있었는지 추적 — 확장 슬롯 판정용.
        tick()에서 주기 호출. 슬롯이 하나라도 비면 타이머를 리셋해서, 확장은
        '오래 만석이었다'가 확인된 경우에만 열린다."""
        if len(self.holdings) >= MAX_HOLDINGS:
            if self._soft_cap_full_since is None:
                self._soft_cap_full_since = now
        else:
            self._soft_cap_full_since = None

    def occupied_slots(self) -> int:
        """보유 + 매수 진행중(pending) 합계 — 오버부킹 방지 (2026-08-01).

        기존엔 len(self.holdings)만 셌다. 그런데 _execute_buy는 주문 REST가
        끝나야 holdings에 넣는데, 그 사이(429 재시도 시 2초 blocking sleep까지
        포함) 종목은 pending에만 있다. on_condition_hit과 watchlist_reentry는
        각각 별도의 asyncio.to_thread에서 도므로, 급등이 동시에 여러 종목에서
        터지면 두 스레드가 같은 '마지막 한 칸'을 동시에 통과해 상한을 넘겨
        매수할 수 있다 — 여러 종목이 한꺼번에 터지는 상황이 바로 우선순위가
        필요한 상황이라, 순위 로직을 넣기 전에 이것부터 막아야 한다.
        """
        return len(set(self.holdings) | self.pending)

    def count_holdings_by_strategy(self, sub: str) -> int:
        """해당 전략의 점유 슬롯 수 (보유 + 매수 진행중).

        (2026-08-01) pending 매수분도 센다 — 위 occupied_slots와 같은 이유.
        _pending_strategy는 _execute_buy가 주문 직전에 기록하고 finally에서 지운다.
        """
        n = sum(1 for h in self.holdings.values() if h.get("sub_strategy") == sub)
        n += sum(
            1 for code, s in self._pending_strategy.items()
            if s == sub and code not in self.holdings
        )
        return n

    def phase1a_max_slots(self) -> int:
        """1A 상한 — 10:30 이후에는 노는 Pullback 슬롯을 흡수 (2026-08-01).
        근거는 PHASE1A_MAX_SLOTS_LATE 정의부 주석 참고."""
        return (
            PHASE1A_MAX_SLOTS if self._now().time() < PULLBACK_END
            else PHASE1A_MAX_SLOTS_LATE
        )

    def can_buy_phase1a(self, info: dict | None = None) -> bool:
        # 1A: 09:00~14:50 (evaluate_1a_leading_strength, 체결강도 단독)
        return (
            self.can_buy_more(info, "1A")
            and self.count_holdings_by_strategy("1A") < self.phase1a_max_slots()
            and GROUP_A_START <= self._now().time() < PHASE1A_END
        )

    def can_buy_pullback(self, info: dict | None = None) -> bool:
        # 눌림목: 09:00~10:30, 1A와 시간대는 겹치지만 슬롯은 별도(sub_strategy="1A_눌림")
        return (
            self.can_buy_more(info, "1A_눌림")
            and self.count_holdings_by_strategy("1A_눌림") < PULLBACK_MAX_SLOTS
            and GROUP_A_START <= self._now().time() < PULLBACK_END
        )

    # ========================================
    # 진입 진단 (2026-08-01 신규)
    # ========================================
    # 사유 원문 -> 분류. 앞에서부터 먼저 걸리는 것을 쓴다.
    # (분류, 원문에 포함된 문자열들)
    _REJECT_RULES = (
        ("대량체결 부족", ("대량체결 부족",)),
        ("체결틱 부족(강도판단불가)", ("체결틱 부족",)),
        ("체결강도 미달", ("체결강도 미달", "체결강도 지속 미달")),
        ("시가급등 보류", ("시가대비",)),
        ("저유동성 보류", ("저유동성",)),
        ("지수 방어(HALT)", ("전면 매매 중단",)),
        ("데이터소스 없음", ("데이터 소스 없음",)),
        ("눌림 미충족", ("눌림 미성립", "반등 미확인")),
        ("VWAP 탈락", ("VWAP",)),
        ("점수 미달", ("점수 부족", "점수 미달")),
        ("슬롯 부족", ("슬롯 부족",)),
        ("재매수 차단", ("재매수", "쿨다운", "매도 차단", "손절 종목")),
        ("조건식 지연(09:20)", ("조건식 지연",)),
        ("장시작 전 대기", ("장 시작 전",)),
        ("매수 컷오프", ("컷오프", "상한",)),
    )
    # '코드/인프라 이상'을 의심해야 하는 분류 — 정상 필터링과 구분해서 경고한다.
    _REJECT_INFRA = ("체결틱 부족(강도판단불가)", "데이터소스 없음")

    @classmethod
    def _reject_category(cls, reason: str) -> str:
        r = reason or ""
        for label, keys in cls._REJECT_RULES:
            if any(k in r for k in keys):
                return label
        return "기타"

    def _note_reject(self, stock_code: str, reason: str):
        """후보가 이번 평가에서 매수되지 않은 사유를 기록 (진단 전용).

        핫패스에서 불리므로 문자열 분류 외에 아무 것도 하지 않는다. 락도 걸지
        않는다 — 여러 워커 스레드가 동시에 써도 dict 대입은 원자적이고,
        진단용 근사치라 정확도보다 오버헤드 0이 중요하다.
        """
        if not stock_code or not reason:
            return
        try:
            self._last_reject[stock_code] = (
                self._reject_category(reason), str(reason)[:120], self._now()
            )
        except Exception:
            pass

    def build_entry_diagnostics(self) -> str:
        """장중 진입 진단 요약 (텔레그램용). main.task_entry_diagnostics가 호출.

        "조건검색엔 계속 잡히는데 왜 안 사는가"를 세 가지로 나눠 보여준다:
          1) 슬롯/시간 같은 구조적 이유인가
          2) 진입 필터가 정상적으로 거르는 중인가 (사유별 종목 수)
          3) 데이터가 안 들어와서 판단 자체를 못 하는가 (=코드/구독 이상 의심)
        """
        now = self._now()
        held = len(self.holdings)
        n_1a = self.count_holdings_by_strategy("1A")
        n_pb = self.count_holdings_by_strategy("1A_눌림")
        cands = [c for c in self.watch_list_today
                 if c not in self.holdings and c not in self.pending]

        lines = [
            f"📊 [진입 진단] {now.strftime('%H:%M')}",
            f"후보 {len(cands)}종목 | 보유 {held}/{MAX_HOLDINGS} "
            f"(1A {n_1a}/{self.phase1a_max_slots()}, 눌림 {n_pb}/{PULLBACK_MAX_SLOTS})",
        ]

        # 1) 매수 가능 여부 자체
        gates = []
        if not self.risk_can_trade():
            gates.append("MDD 일손실 차단")
        if self._get_market_defense_mode() == "HALT":
            gates.append("지수 HALT")
        if now < self.quarantine_until:
            gates.append("WS 재연결 격리")
        if now.time() >= ENTRY_HARD_CUTOFF:
            gates.append("15:10 하드컷오프")
        if gates:
            lines.append(f"⛔ 전면 차단: {', '.join(gates)}")
        elif not self.can_buy_phase1a():
            lines.append("🔒 1A 슬롯 없음 (슬롯이 비면 자동 재평가)")
        else:
            lines.append("🟢 1A 슬롯 여유 있음 — 트리거 대기 중")

        # 2) 사유별 집계 (최근 10분 내 기록만 — 오래된 건 이미 상황이 바뀜)
        recent, counts = 0, {}
        for code in cands:
            rec = self._last_reject.get(code)
            if not rec:
                continue
            cat, _, ts = rec
            if (now - ts).total_seconds() > 600:
                continue
            recent += 1
            counts[cat] = counts.get(cat, 0) + 1
        if counts:
            lines.append("미체결 사유(최근 10분):")
            for cat, n in sorted(counts.items(), key=lambda kv: -kv[1])[:6]:
                lines.append(f"  {n:>3}종목  {cat}")
        elif cands:
            lines.append("⚠️ 후보는 있는데 최근 10분간 평가 기록이 없음 "
                         "— 재평가 루프 정지 의심")

        # 3) 인프라 이상 신호
        warns = []
        infra_n = sum(n for c, n in counts.items() if c in self._REJECT_INFRA)
        if infra_n:
            warns.append(f"체결 데이터 없음 {infra_n}종목 (0B 구독/WS 확인)")

        if self.phase1b and getattr(self.phase1b, "trade_flow", None):
            silent = [c for c in cands
                      if self.phase1b.is_watching(c)
                      and self.phase1b.trade_flow.tick_count(c, 120) == 0]
            if silent:
                warns.append(
                    f"감시중인데 체결틱 0인 종목 {len(silent)}개: "
                    f"{', '.join(silent[:5])}{'...' if len(silent) > 5 else ''}"
                )
            unwatched = [c for c in cands if not self.phase1b.is_watching(c)]
            if unwatched:
                warns.append(
                    f"후보인데 감시 미시작 {len(unwatched)}개 "
                    f"(1A 평가에 도달 못 함): {', '.join(unwatched[:5])}"
                    f"{'...' if len(unwatched) > 5 else ''}"
                )

        # 슬롯이 비어있는데 오래 매수가 없으면 파이프라인 이상 의심
        if not gates and self.can_buy_phase1a() and cands:
            since = self._last_buy_at
            idle_min = (now - since).total_seconds() / 60 if since else None
            if since is None and now.time() >= time(9, 30):
                warns.append("오늘 매수 0건 — 진입 경로 전체 점검 필요")
            elif idle_min is not None and idle_min >= 60:
                warns.append(f"슬롯 여유 상태로 {idle_min:.0f}분째 매수 없음")

        for w in warns:
            lines.append(f"⚠️ {w}")

        # 4) 성과 기반 문턱 조정 현황 (제안 B 효과 가시화)
        tiers = []
        for key in ("1A", "1A_눌림"):
            t = self.perf.tier(key)
            if t != "NEUTRAL":
                tiers.append(f"{key}={t}")
        for name in ("주도주상위", "돌파자동매매용", "눌림목자동"):
            t = self.perf.tier(COND_PERF_PREFIX + name)
            if t != "NEUTRAL":
                tiers.append(f"{name}={t}")
        if tiers:
            lines.append(f"성과 등급: {', '.join(tiers)}")

        return "\n".join(lines)

    def candidate_tier(self, stock_code: str) -> float:
        """종목 우선순위 지표 (2026-08-01 신규). 클수록 좋은 자리.

        tier = 거래대금 가속도 x 체결강도배수
          - 거래대금 가속도: 최근 30초 거래대금 / (최근 120초 평균 30초분).
            1.0 = 균일, 3.0 = 지금 3배로 몰리는 중.
          - 체결강도배수: 최근 30초 강도 / 100. 매수 우세면 1 초과.

        **두 항 모두 그 종목 자신의 최근 이력 대비 비율**이라는 게 핵심이다.
        절대 거래대금으로 줄을 세우면 대형주(돌파자동매매용)가 항상 이기고,
        조건검색이 굳이 찾아준 소형 급등주(주도주상위)는 영영 슬롯을 못 잡는다.
        자기 대비로 재면 두 조건검색식의 종목이 같은 잣대에서 비교되므로
        **조건검색식별 우선순위라는 개념 자체가 필요 없어진다.**

        비용: 이미 메모리에 있는 틱 버퍼만 읽는다 — REST 0콜, 대기 0초.
        그래서 매수 트리거 시점에 호출해도 진입이 단 1ms도 늦지 않는다.

        반환 0.0 = 판단 불가(틱 부족). 호출부는 이 경우 **남의 슬롯을 빼앗는
        것만 포기**하고, 빈 슬롯 매수는 정상 진행한다 — '모름'이 매수를
        막아버리면 우선순위를 도입한 대가로 매매를 잃는 셈이 된다.
        """
        if not (self.phase1b and getattr(self.phase1b, "trade_flow", None)):
            return 0.0
        tf = self.phase1b.trade_flow
        try:
            if tf.tick_count(stock_code, PHASE1A_TIER_LONG_SEC) < PHASE1A_TIER_MIN_TICKS:
                return 0.0
            accel = tf.value_acceleration(
                stock_code, PHASE1A_TIER_SHORT_SEC, PHASE1A_TIER_LONG_SEC
            )
            if accel <= 0:
                return 0.0
            strength = tf.compute_strength(
                stock_code, PHASE1A_TIER_SHORT_SEC, min_ticks=PHASE1A_TIER_MIN_TICKS
            )
            if strength <= 0:
                return 0.0
            # [제안 A] 신호 세기 가중 — 단일 대량체결이 터진 자리를 우대한다.
            max_single = tf.max_single_trade_value(stock_code, PHASE1A_TIER_SHORT_SEC)
            if max_single >= PHASE1A_SINGLE_TRADE_VALUE:
                signal_mult = PHASE1A_TIER_SINGLE_MULT
            elif max_single >= PHASE1A_BURST_TRADE_VALUE:
                signal_mult = PHASE1A_TIER_BURST_MULT
            else:
                signal_mult = 1.0
            return accel * (strength / 100.0) * signal_mult
        except Exception:
            logger.exception("[%s] tier 계산 실패 -> 판단 불가(0)", stock_code)
            return 0.0

    def _try_1a_priority_upgrade(self, candidate_code: str, candidate_tier: float) -> bool:
        """슬롯이 꽉 찬 순간에만 도는 '종목 우선순위' 교체 (2026-08-01 전면 재설계).

        설계 의도 — 조건검색식 우선순위가 아니라 **종목 우선순위**를, 그것도
        **진입 지연 0으로** 구현한다. 핵심은 세 가지다:

        ① 이 함수는 슬롯이 꽉 찼을 때만 호출된다. 실측상 슬롯 사용률이 10%라
           대부분의 트리거는 여기 오지도 않고 곧장 매수된다 — 급등 포착 속도가
           우선순위 도입 때문에 느려지는 일은 구조적으로 없다.
        ② 후보를 모아 정렬하지 않는다. tier는 이미 틱 버퍼에 계산 재료가 다
           들어있어 조회만 하면 되므로(REST 0콜), 호출돼도 즉시 끝난다.
        ③ 비교 기준이 **자기 대비 비율**(candidate_tier)이라 소형 급등주와
           대형주가 공평하게 겨룬다.

        안전장치:
          - 교체 대상은 "아직 결과가 안 난 자리"(순손익 ±PHASE1A_PRIORITY_FLAT_BAND)
            로 제한 — 오르고 있는 포지션을 순간 스파이크로 빼앗지 않는다.
          - tier를 모르는(0.0) 후보는 교체를 시도하지 않는다.
          - 최소보유 시간 미달 포지션은 제외(사자마자 되파는 수수료 낭비 방지).
        기존 09:00~09:10 제한은 해제했다 — 슬롯 만석 자체가 이미 강한 게이트라
        시간으로 또 막을 이유가 없고, 오히려 10:30 이후 좋은 자리를 놓쳤다.

        반환 True = 자리를 하나 비웠음(호출부가 can_buy_phase1a를 재확인해 매수).
        """
        if candidate_tier <= 0:
            return False
        now_dt = self._now()
        if not (GROUP_A_START <= now_dt.time() < PHASE1A_END):
            return False

        best_code, best_tier, best_price = None, None, None
        for code, pos in self.holdings.items():
            if pos.get("sub_strategy") != "1A":
                continue
            if code in self.pending or code in self.sell_blocked:
                continue
            buy_time = pos.get("buy_time")
            if buy_time is None:
                continue
            if (now_dt - buy_time).total_seconds() < PHASE1A_PRIORITY_MIN_HOLD_SEC:
                continue

            # 결과가 이미 난 자리(오르거나 밀린 자리)는 건드리지 않는다.
            price = self._fresh_tick_price(code)
            if not price:
                continue  # 실시간가를 모르면 판단 보류 (REST까지 쓰진 않는다)
            if abs(self._net_rate(pos.get("buy_price") or 0, price)) > PHASE1A_PRIORITY_FLAT_BAND:
                continue

            tier = self.candidate_tier(code)
            if best_tier is None or tier < best_tier:
                best_code, best_tier, best_price = code, tier, price

        if best_code is None:
            return False
        if candidate_tier < max(best_tier, 0.0) * PHASE1A_PRIORITY_MARGIN:
            return False

        logger.info(
            "[%s] 1A 종목 우선순위 교체 — 후보 tier %.2f >= 최약보유 %s tier %.2f x%.1f "
            "(정체 구간이라 교체 가능) -> 매도 후 신규매수 시도",
            candidate_code, candidate_tier, best_code, best_tier,
            PHASE1A_PRIORITY_MARGIN,
        )
        self._execute_sell(
            best_code, best_price,
            f"1A 우선순위 교체 (후보 {candidate_code} tier {candidate_tier:.2f} "
            f">= 보유 tier {best_tier:.2f} x{PHASE1A_PRIORITY_MARGIN:.1f})",
        )
        return True

    def can_buy_phase1b(self) -> bool:
        # 1B/1L은 점수 컷라인이 없는 실시간 틱 경로라 컷라인 조정 대상은 아니지만,
        # COLD일 때 공유 슬롯 마지막 칸을 양보하는 것은 동일하게 적용된다.
        return (
            self.can_buy_more(None, "1B")
            and self.count_holdings_by_strategy("1B") < PHASE1B_MAX_SLOTS
            and GROUP_A_START <= self._now().time() < PULLBACK_END
        )

    def can_buy_leading(self) -> bool:
        # 주도주 우선 진입: 09:00~10:50
        return (
            self.can_buy_more(None, "1L")
            and self.count_holdings_by_strategy("1L") < LEADING_MAX_SLOTS
            and LEADING_START <= self._now().time() < LEADING_END
        )

    # ========================================
    # 쿨다운 / 차단
    # ========================================
    def _is_rebuy_blocked(self, stock_code: str) -> tuple[bool, str]:
        if stock_code in self.sell_blocked:
            return True, "매도 차단 (영구실패)"
        if stock_code in self._stoploss_blocked:
            return True, "손절 종목 당일 재매수 금지"
        cnt = self._buy_count_today.get(stock_code, 0)
        if cnt >= MAX_BUYS_PER_STOCK:
            return True, (
                f"재매수 상한 초과 (당일 {cnt}회 매수 = 최초 1회 + 재매수 "
                f"{MAX_BUYS_PER_STOCK - 1}회 소진)"
            )
        if stock_code in self.sold_at:
            elapsed = self._now() - self.sold_at[stock_code]
            if elapsed < REBUY_COOLDOWN:
                remaining = REBUY_COOLDOWN - elapsed
                return True, f"쿨다운 (잔여 {int(remaining.total_seconds())}초)"
        return False, ""

    # ========================================
    # 진입
    # ========================================
    def on_condition_hit(
        self,
        stock_code: str,
        stock_name: str,
        is_surge: bool = False,
        cond_name: str = "알수없음",
    ):
        # [추가] 신호가 들어왔을 때 조건명(cond_name)을 포함해 로그 출력
        logger.info(f"📈 편입 신호: {stock_code} ({stock_name}) - 조건: {cond_name}")

        if stock_code in self.holdings or stock_code in self.pending:
            return

        # ⚠️ 기록(이름/조건명)은 시간 게이트보다 **먼저** 한다 (2026-08-01 수정).
        # 기존엔 get_current_phase()가 None이면(=09:00 이전) 여기 도달하기 전에
        # return 해서, 스케줄러를 08:59로 앞당긴 뒤로는 기동 스냅샷 종목이
        # 이름·조건명·워치리스트 어디에도 남지 않고 통째로 사라졌다.
        # 실시간 편입(type='02')까지 유실되고 있던 상태(kiwoom_ws 수정 전)라,
        # 08:59 기동 + 이 게이트 = 하루 종일 조건검색 종목 0건이 될 수 있었다.
        self._stock_names[stock_code] = stock_name
        # 이미 실제 조건명이 기록돼 있으면 "기타"/"알수없음" 같은 부실한 값으로
        # 덮어쓰지 않음 (2026-07-30). 실시간 WS 편입 이벤트(_on_signal)가 cond_seq를
        # 못 읽어 "기타"로 넘어오는 경우가 있는데, 이게 초기 스냅샷이 이미 정확히
        # 기록해둔 "주도주상위+..." 같은 값을 덮어써버리면 OTHER_COND_START(09:20)
        # 게이트가 주도주상위 종목까지 잘못 지연시킴 — 오늘 실전에서 이 때문에
        # SK이터닉스 등 9종목이 09:01~09:20 사이 18분간 평가 자체가 멈췄던 것 확인.
        # (2026-07-31) 돌파자동매매용처럼 "주도주상위"가 아닌 조건식이 먼저 걸린
        # 종목에 나중에 주도주상위 신호가 와도, 기존 first-write-wins 로직이라
        # cond_name이 영영 갱신 안 돼 09:20까지 잘못 지연됐음(주도주상위+돌파
        # 자동매매용 콜라보 사용 시작하며 발견) — "주도주상위"가 새로 포함되면
        # 예외적으로 승격시켜 병합한다.
        existing_cond = self._cond_names.get(stock_code)
        if not existing_cond or existing_cond in ("기타", "알수없음"):
            self._cond_names[stock_code] = cond_name
        elif "주도주상위" in cond_name and "주도주상위" not in existing_cond:
            self._cond_names[stock_code] = f"{existing_cond}+{cond_name}"

        # 거래대금 폭발 이력(explosion_scorer) 준비는 여기서 더 이상 안 함 —
        # 종가베팅 스캐너(main.py task_closing_bet_scanner, 14:50)에서만 쓰이는
        # 데이터인데 조건검색 걸릴 때마다(하루 수십 회) 미리 계산해두느라
        # REST 호출이 과다해지던 문제(429 다발)가 있어서, 실제 필요한 시점인
        # 14:50 스캔 때 그 시점 후보 종목들만 대상으로 계산하도록 이관함.
        # (2026-07-28: 조건검색에 N봉 신고가+거래량 필터를 사용자가 직접 추가해서
        # 1차 필터링이 이미 상류에서 되고 있는 것도 이관 결정의 근거)

        blocked, reason = self._is_rebuy_blocked(stock_code)
        if blocked:
            logger.info("[%s] %s 매수 차단: %s", stock_code, stock_name, reason)
            return

        try:
            now_t = self._now().time()

            # 장 시작 전(08:59 기동 등)에는 평가만 미루고 후보로는 남긴다
            # (2026-08-01). watch_list_today에 들어가면 09:00부터
            # task_watchlist_reentry(15초 주기)가 자동으로 재평가한다 —
            # '버리는' 게 아니라 '미루는' 것이 핵심.
            if now_t < GROUP_A_START:
                self.watch_list_today.add(stock_code)
                self._note_reject(
                    stock_code,
                    f"장 시작 전 편입 — {GROUP_A_START.strftime('%H:%M')}부터 평가",
                )
                logger.info(
                    "[%s] %s 장 시작 전 편입 — %s부터 평가 대기 (후보 등록)",
                    stock_code, stock_name, GROUP_A_START.strftime("%H:%M"),
                )
                return
            if now_t >= PHASE1A_END:
                return

            candles = self._get_merged_candles(stock_code, interval=1, count=15)
            if not candles or len(candles) < VOLUME_LOOKBACK + 1:
                logger.warning(
                    "[%s] 분봉 부족 (%d개)", stock_code, len(candles) if candles else 0
                )
                return

            current_price = int(candles[0].get("close", 0))
            # 당일 시가 — candles는 **내림차순**(최신->과거)이라 기존의
            # candles[-1]["open"]은 '가장 오래된 봉의 시가'였고, 개장 직후엔
            # API가 전일 봉까지 채워 주므로 실제로는 **전일 시가**였다
            # (07-30 로그 실측: 09:01 시점 candles[-1] = 20260729 15:09 봉).
            # 그 값으로 "시가대비 +5% 매수보류"를 재던 탓에 사실상
            # '전일종가 대비 +5%'가 되어 개장 초반 주도주가 전부 잘렸다.
            open_price = self._today_open(candles)
            if open_price > 0:
                self._opening_prices.setdefault(stock_code, open_price)

            self._evaluate_1a_pullback_entry(
                stock_code, stock_name, 1, candles, current_price, open_price, now_t
            )

        except Exception as e:
            logger.exception("[%s] on_condition_hit 실패: %s", stock_code, e)
            SystemEventRepository.log("STRATEGY_ERROR", f"{stock_code}: {e}", "ERROR")
            _notify(f"전략 에러\n{stock_code}: {e}")

    def _evaluate_1a_pullback_entry(
        self, stock_code, stock_name, phase, candles, current_price, open_price, now_t
    ) -> bool:
        # candles=None 허용 (2026-08-01) — 새 1A는 순수 틱 계산이라 분봉이
        # 전혀 필요 없다. 재평가 루프(watchlist_reentry, 15초 주기)가 후보마다
        # 분봉을 긁으면 429 예산이 그대로 녹는다(후보 40종목이면 한 사이클에
        # REST 40콜 = 자체 상한 분당 100콜의 절반 이상). Pullback 분기에
        # 도달했을 때만 지연 로딩한다.
        """1A + Pullback 평가/매수 시도 (슬롯 없으면 watch_list에만 기록).
        on_condition_hit(최초 편입 시점)과 watchlist_reentry(슬롯 재확보 시 재평가)
        양쪽에서 공유하는 로직 — 2026-07-28 분리(슬롯 꽉 찼을 때 1A/Pullback 후보가
        재평가 없이 영구히 방치되던 문제 수정, core/watchlist_reentry.py 참고).
        반환: 매수 실행했으면 True."""
        # watchlist_reentry 경로는 on_condition_hit과 달리 호출 전에 재매수 차단을
        # 확인하지 않아서, 손실차단/쿨다운 중인 종목이 슬롯 재확보 시 바로 재매수될
        # 수 있었음 — 공유 진입점인 여기서 한 번 더 확인. (2026-07-29)
        blocked, reason = self._is_rebuy_blocked(stock_code)
        if blocked:
            logger.info("[%s] %s 매수 차단: %s", stock_code, stock_name, reason)
            self._note_reject(stock_code, reason)
            return False

        # 주도주상위/눌림목자동(IMMEDIATE_COND_NAMES)은 GROUP_A_START(09:00)부터
        # 바로 평가, 그 외 조건검색식(돌파자동매매용 등)은 09:20 이전이면
        # 아직 평가하지 않음(2026-07-29, 2026-07-31 눌림목자동 추가). on_condition_hit/
        # watchlist_reentry 공유 진입점이라 여기 한 곳에 둬야 두 경로 다 일관되게 걸림 —
        # 특히 task_watchlist_reentry가 15초마다 재시도하므로 on_condition_hit
        # 쪽에서만 막으면 곧바로 재평가돼서 지연이 무의미해짐.
        # watch_list_today에는 넣어둬서 09:20이 지나면 watchlist_reentry가
        # 자연히 다시 평가하게 함(단순 차단이 아니라 지연 평가).
        cond_name = self._cond_names.get(stock_code, "")
        if not any(n in cond_name for n in IMMEDIATE_COND_NAMES) and now_t < OTHER_COND_START:
            self.watch_list_today.add(stock_code)
            self._note_reject(
                stock_code,
                f"조건식 지연 — {cond_name or '기타'}는 "
                f"{OTHER_COND_START.strftime('%H:%M')}부터 평가",
            )
            return False

        # ── 전략 라우팅: 상호배타 (2026-08-01 사용자 지정) ──────────────
        # "전략이 오락가락하지 않도록 눌림목매수는 눌림목자동 조건검색에서만".
        # 기존엔 눌림목자동 '단독'일 때만 1A를 건너뛰었고, 다른 조건식과 겹친
        # 종목은 1A와 Pullback 둘 다 평가받아서 같은 종목이 어느 날은 1A로,
        # 어느 날은 눌림으로 잡히는 일관성 없는 진입이 났다. 이제 완전히 나눈다:
        #     cond_name에 "눌림목자동" 포함 -> Pullback 전용 (1A 평가 안 함)
        #     그 외                          -> 1A 전용    (Pullback 평가 안 함)
        # 전제가 정반대인 두 검색식(눌림목자동=당일고가 -3% 되돌림 /
        # 주도주상위·돌파=상승 모멘텀)을 각자 맞는 전략에만 연결하는 것이기도 하다.
        is_pullback_source = "눌림목자동" in cond_name

        # ==========================================
        # 1A: 체결강도 단독 즉시진입 (09:00~14:50) — 2026-07-31 전면 단순화
        # ==========================================
        # 기존엔 주도주상위 소스만 evaluate_1a_leading_strength(체결강도만
        # 확인)를 쓰고 나머지(돌파자동매매용/기타)는 evaluate_new_intensity_
        # strategy(거래량증가지속+체결강도지속+점수)를 썼는데, 사용자 지정으로
        # 이제 소스 구분 없이 전부 체결강도 단독 방식으로 통일한다 — "이미
        # 조건검색식에서 걸러졌으니 우리 쪽 추가 필터(거래량증가지속/점수)가
        # 오히려 중복검증으로 지연만 유발한다"는 판단을 돌파자동매매용까지
        # 확장 적용. 대가: 거래량/MA/양봉 등 품질필터가 전부 빠지고 전일종가
        # 12% 상한만 안전장치로 남음(사용자가 인지하고 수용한 트레이드오프 —
        # 핵심 목표는 "고가 아닌 트리거 지점에서 매수").
        # evaluate_new_intensity_strategy 자체는 삭제하지 않고 아래 정의부에
        # 그대로 남겨둠(주석으로 보류, 필요시 언제든 되돌릴 수 있게).
        if current_price > 0 and not is_pullback_source:
            ok, info = self.evaluate_1a_leading_strength(
                stock_code, current_price, open_price, cond_name
            )
            self._record_watch_list(stock_code, stock_name, phase, info, cond_name)
            if ok:
                # ── 종목 우선순위 (2026-08-01 재설계) ──────────────────
                # **슬롯에 여유가 있으면 순위를 아예 보지 않는다** — 곧장
                # 아래 can_buy_phase1a로 내려가 즉시 매수한다(딜레이 0).
                # 슬롯이 꽉 찬 순간에만 tier를 조회해(메모리 연산, REST 0콜)
                # 가장 약하면서 아직 결과가 안 난 자리와 겨룬다.
                # 이렇게 나눠야 "우선순위 때문에 급등 포착이 늦어지는" 문제가
                # 원천적으로 생기지 않는다.
                slots_full = (
                    self.count_holdings_by_strategy("1A") >= self.phase1a_max_slots()
                    or self.occupied_slots() >= MAX_HOLDINGS
                )
                if slots_full:
                    self._try_1a_priority_upgrade(
                        stock_code, self.candidate_tier(stock_code)
                    )
                if self.can_buy_phase1a(info):
                    self._execute_buy(stock_code, stock_name, phase, info, sub_strategy="1A")
                    return True
                self._note_reject(
                    stock_code,
                    f"슬롯 부족 (1A {self.count_holdings_by_strategy('1A')}"
                    f"/{self.phase1a_max_slots()}, 전체 {self.occupied_slots()}/{MAX_HOLDINGS})",
                )
            else:
                self._note_reject(stock_code, info.get("reason", ""))

        # ==========================================
        # Pullback: 눌림목 반등 점수 + VWAP AND (09:00~10:30, 1A와 시간대 겹침/슬롯 별도)
        # ==========================================
        if is_pullback_source and now_t < PULLBACK_END:
            # 분봉 지연 로딩 (2026-08-01) — Pullback은 분봉이 반드시 필요하다.
            if candles is None:
                try:
                    candles = self._get_merged_candles(stock_code, interval=1, count=15)
                except Exception as e:
                    logger.warning("[%s] pullback 분봉 조회 실패: %s", stock_code, e)
                    return False
            if not candles or len(candles) < VOLUME_LOOKBACK + 1:
                return False

            # OBV(누적거래량) 모멘텀 — 점수 요소로 씀(2026-07-29). VWAP과 동일하게
            # 당일 전용 캔들로 계산(전일 데이터 섞이는 _get_merged_candles 절대
            # 안 씀), 아래 VWAP 필터에서 같은 캔들을 재사용해 REST 추가호출 없음.
            today_candles = None
            obv_mom = 0.0
            try:
                today_candles = self.api.get_minute_candles(
                    stock_code, interval=1, count=400
                )
                obv_mom = obv_momentum(today_candles, lookback=OBV_LOOKBACK)
            except Exception as e:
                logger.warning("[%s] OBV 계산 실패, 무점수 처리: %s", stock_code, e)

            # skip_setup_check=True 전면 적용(2026-07-31, 사용자 지정) — 기존엔
            # cond_name=="눌림목자동"인 종목만 로컬 되돌림 재검증(_pullback_setup,
            # 10분짜리 로컬 스케일)을 건너뛰었는데, 413630처럼 다른 소스로 온
            # 종목도 같은 이유(하루 스케일 급등이 10분 로컬 잣대와 안 맞음)로
            # 로컬 재검증에 20분씩 갇히는 걸 확인해서 소스 구분 없이 전부
            # 생략하기로 함. 반등 확인(양봉+5MA돌파)+점수(거래량/강도/OBV)+
            # VWAP AND필터는 그대로 유지 — 완전 무필터가 아니라 "로컬 되돌림
            # 폭이 1~3%인지"라는, 급등형 종목엔 안 맞는 조건 하나만 뺀 것.
            ok, info = self.evaluate_pullback(
                candles, stock_code, obv_mom, skip_setup_check=True
            )
            self._record_watch_list(stock_code, stock_name, phase, info, cond_name)

            # 체결틱 수집 시작 (2026-08-01 재정의) — 1B가 비활성화되면서
            # start_watching은 더 이상 "1B 감시 시작"이 아니라 **체결강도/호가
            # 데이터 수집 시작**을 뜻한다. Pullback 점수의 강도 항목이 실제
            # 값을 가지려면 여기서 켜져 있어야 하므로, 예전의 can_buy_phase1b()
            # 게이트(1B 슬롯/시간창에 종속)는 제거하고 무조건 켠다.
            if self.phase1b and not self.phase1b.is_watching(stock_code):
                self.phase1b.start_watching(stock_code)

            if ok:
                if self._apply_vwap_filter(
                    stock_code, "pullback", current_price, info,
                    today_candles=today_candles,
                ):
                    if self.can_buy_pullback(info):
                        self._execute_buy(
                            stock_code, stock_name, phase, info, sub_strategy="1A_눌림"
                        )
                        # (2026-08-01) 매수 후 stop_watching 하던 것을 제거.
                        # 예전엔 "1B가 이 종목을 또 사는 것"을 막으려는 조치였는데
                        # 1B는 이제 비활성화(PHASE1B_ENABLED=False)라 그 위험이
                        # 없고, 반대로 감시를 끄면 trade_flow.reset()이 호출돼
                        # **보유 중 체결강도를 볼 수 없게 된다** — 동적 익절캡
                        # 상향/조기확정, 손실반등 매도, 슬롯교체가 전부 이 값을
                        # 쓰므로 보유하는 동안은 반드시 감시를 유지해야 한다.
                        # 청산 후 정리는 tick()의 _cleanup_watched가 담당.
                        return True
                    logger.info("[%s] pullback 조건 OK but 슬롯 부족", stock_code)
                    self._note_reject(
                        stock_code,
                        f"슬롯 부족 (눌림 {self.count_holdings_by_strategy('1A_눌림')}"
                        f"/{PULLBACK_MAX_SLOTS}, 전체 {self.occupied_slots()}/{MAX_HOLDINGS})",
                    )
                else:
                    self._note_reject(stock_code, "VWAP 필터 탈락")
            elif info.get("reason"):
                logger.info(
                    "[%s] %s pullback 미충족: %s", stock_code, stock_name, info.get("reason")
                )
                self._note_reject(stock_code, info.get("reason", ""))

        return False

    # ========================================
    # 실시간 콜백
    # ========================================
    def on_trade(self, parsed_trade: dict, now: float = None):
        code = parsed_trade.get("stock_code")
        if not code:
            return

        # ⚠️ 체결틱 적재는 **가장 먼저** 한다 (2026-08-01 수정).
        # 기존엔 보유 종목이면 on_price_update 후 곧바로 return 해서
        # trade_flow.add_tick까지 도달하지 못했다 — 그래서 매수하는 순간부터
        # 그 종목의 틱 버퍼가 얼어붙고, 10초만 지나면 compute_strength가
        # 중립값(100)만 반환했다. 그 결과 동적 익절캡 상향/조기확정,
        # 동적캡 즉시매도, 손실반등 하이브리드 매도가 전부 '판단 불가'로
        # 빠져 사실상 작동하지 않았다(07-31에 매수 직후 66초 안에만 발동한
        # 이유도 이것 — 매수 전에 쌓아둔 틱이 남아있던 구간뿐이었다).
        if self.phase1b and self.phase1b.is_watching(code):
            try:
                self.phase1b.trade_flow.add_tick(
                    code,
                    parsed_trade.get("price", 0),
                    parsed_trade.get("side", "neutral"),
                    parsed_trade.get("volume", 0),
                    now=now,
                )
            except Exception:
                logger.exception("[%s] 체결틱 적재 실패", code)

        if code in self.holdings:
            price = parsed_trade.get("price")
            if price:
                self.on_price_update(code, price)
            return

        # 1L 전체 주석처리 (2026-07-31, 사용자 지정) — 새 1A(evaluate_1a_leading_strength,
        # 체결강도 100 이상 1분 유지)가 1L(테마리더+강도100 2분)과 사실상 중복
        # 설계라서 보류함. 1분<2분이라 새 1A가 항상 먼저 사가 1L은 실질적으로
        # 죽은 코드가 되는 상태였음. 삭제 대신 아래 문자열 리터럴(주석 처리)로
        # 남겨둠 — 재활성화하려면 이 블록을 다시 살아있는 코드로 되돌리면 됨.
        if False:
            """
            now_dt = self._now()
            now_t = now_dt.time()
            strength = parsed_trade.get("strength") or 0.0

            # 3개 하위조건을 개별로 평가 — 어느 조건이 막고 있는지 알기 위해
            # (2026-07-30 진단 로깅). 기존엔 and로 묶여 있어서 실패 원인이
            # 로그에 전혀 남지 않았고, 1L이 연일 0건인 이유를 알 수 없었음.
            theme_ok = self.theme_mgr.is_leading_theme_stock(code)
            strength_ok = strength >= LEADING_STRENGTH_MIN
            window_ok = LEADING_START <= now_t < LEADING_END
            qualifies = theme_ok and strength_ok and window_ok

            d = self._l1_diag
            d["ticks"] += 1
            if theme_ok:
                d["theme_ok"] += 1
            if strength_ok:
                d["strength_ok"] += 1
            if window_ok:
                d["window_ok"] += 1
            if qualifies:
                d["both_ok"] += 1

            if not qualifies:
                # 타이머가 돌고 있던 종목이 탈락한 경우만 로깅(=아깝게 놓친 케이스).
                # 애초에 자격 없던 종목은 로그를 남기지 않음(틱마다 쏟아짐).
                prev = self._leading_since.pop(code, None)
                if prev is not None and window_ok:
                    held = (now_dt - prev).total_seconds()
                    self._l1_max_sustain_sec = max(self._l1_max_sustain_sec, held)
                    last = self._l1_reset_logged_at.get(code)
                    if last is None or (now_dt - last).total_seconds() >= 30:
                        self._l1_reset_logged_at[code] = now_dt
                        fail = []
                        if not theme_ok:
                            fail.append("주도테마 이탈")
                        if not strength_ok:
                            fail.append(f"강도 {strength:.0f}<{LEADING_STRENGTH_MIN:.0f}")
                        logger.info(
                            "[%s] 1L 지속 리셋: %.0f초 유지 후 탈락 (%s) — 2분 필요",
                            code, held, ", ".join(fail) or "?",
                        )
            else:
                first_seen = self._leading_since.get(code)
                if first_seen is None:
                    self._leading_since[code] = now_dt
                    first_seen = now_dt
                    logger.info(
                        "[%s] 1L 지속 감시 시작 (테마=%s, 강도=%.0f) — %.0f초 유지 필요",
                        code,
                        self.theme_mgr.code_to_theme.get(code, "?"),
                        strength,
                        LEADING_SUSTAIN.total_seconds(),
                    )

                held = (now_dt - first_seen).total_seconds()
                self._l1_max_sustain_sec = max(self._l1_max_sustain_sec, held)

                if now_dt - first_seen >= LEADING_SUSTAIN:
                    # 지속 조건은 통과 — 이후 슬롯/재매수 게이트에서 막히는지 확인
                    can_buy = self.can_buy_leading()
                    blocked, reason = self._is_rebuy_blocked(code)
                    price = parsed_trade.get("price")
                    if not can_buy or blocked or not price:
                        last = self._l1_block_logged_at.get(code)
                        if last is None or (now_dt - last).total_seconds() >= 60:
                            self._l1_block_logged_at[code] = now_dt
                            if not can_buy:
                                why = (
                                    f"슬롯/시장 게이트 (1L보유 "
                                    f"{self.count_holdings_by_strategy('1L')}/{LEADING_MAX_SLOTS}, "
                                    f"전체 {len(self.holdings)}/{MAX_HOLDINGS})"
                                )
                            elif blocked:
                                why = f"재매수 차단 ({reason})"
                            else:
                                why = "체결가 없음"
                            logger.info(
                                "[%s] 1L 지속 %.0f초 충족했으나 매수 안 됨: %s",
                                code, held, why,
                            )
                    else:
                        stock_name = self._stock_names.get(code, code)
                        theme_name = self.theme_mgr.code_to_theme.get(code, "")
                        logger.info(
                            "🚀 [주도주 우선 진입] %s 테마=%s 강도=%.1f (2분 이상 유지)",
                            code,
                            theme_name,
                            strength,
                        )
                        info = {"current_price": price, "theme": theme_name}
                        self._execute_buy(
                            code, stock_name, phase=1, info=info, sub_strategy="1L"
                        )
                        self._leading_since.pop(code, None)
                        return

            self._maybe_report_1l_diag(now_dt)
            """

        # ── 1B 진입 트리거 [2026-08-01 비활성화, 사용자 지정] ──────────
        # 1A와 방향이 정반대(1A=매수세 폭발 시 진입 / 1B=60초 -2% 급락 시 진입)
        # 라 같은 후보 풀에 동시 적용하면 진입 타이밍이 오락가락한다. 삭제하지
        # 않고 PHASE1B_ENABLED로 가드만 걸어둔다 — True로 바꾸면 그대로 복구됨.
        # 주의: 체결틱 적재(add_tick)는 위쪽으로 이미 옮겼으므로 여기서
        # phase1b.on_trade()를 부르면 **같은 틱이 두 번 쌓여** 체결강도/거래대금이
        # 2배로 부풀려진다. 그래서 복구 시에도 add_tick은 위 한 곳에서만 한다.
        if PHASE1B_ENABLED and self.phase1b and self.phase1b.is_watching(code):
            drop_pct = self.phase1b.trade_flow.get_price_change_pct(
                code, PHASE1B_PULLBACK_WINDOW_SEC, now
            )
            if drop_pct is not None and drop_pct <= PHASE1B_PULLBACK_PCT:
                self._try_phase1b_buy(code, now)

    def _maybe_report_1l_diag(self, now_dt):
        """1L 판정 통계를 10분마다 1회 요약 로깅 (2026-07-30 진단용).
        개별 전이 로그가 하나도 안 찍히는 경우(=자격 갖춘 종목이 아예 없음)를
        구분하기 위함 — "조건이 근처까지 갔는지"를 숫자로 남긴다.
        시간창(09:00~10:50) 밖에서는 의미가 없으므로 보고하지 않는다."""
        if not (LEADING_START <= now_dt.time() < LEADING_END):
            return
        if (now_dt - self._l1_diag_last_report).total_seconds() < 600:
            return
        self._l1_diag_last_report = now_dt
        d = self._l1_diag
        ticks = d["ticks"] or 1  # 0 나눗셈 방지
        logger.info(
            "📊 [1L 진단 10분요약] 틱 %d | 주도테마소속 %d(%.1f%%) | 강도>=%.0f %d(%.1f%%) "
            "| 3조건충족 %d(%.1f%%) | 최장유지 %.0f초/%.0f초필요 | 감시중 %d종목 | 주도테마 %d개",
            d["ticks"],
            d["theme_ok"], d["theme_ok"] / ticks * 100,
            LEADING_STRENGTH_MIN,
            d["strength_ok"], d["strength_ok"] / ticks * 100,
            d["both_ok"], d["both_ok"] / ticks * 100,
            self._l1_max_sustain_sec,
            LEADING_SUSTAIN.total_seconds(),
            len(self._leading_since),
            len(getattr(self.theme_mgr, "leading_themes", []) or []),
        )

    def on_orderbook(self, parsed_orderbook: dict, now: float = None):
        """호가창(0D) 수신 — 매도 1~3호가 잔량을 최신 상태로 유지한다.

        (2026-08-01 재활성화) 07-31에 WallDetector(매도벽 FSM)를 걷어내면서
        이 콜백이 통째로 `return`이 되어 OrderbookTracker가 **한 번도 갱신되지
        않는** 상태였다. 이제 1A 하이브리드 주문(호가가 두툼하면 시장가, 텅
        비었으면 지정가)이 이 데이터를 쓰므로 다시 적재한다.
        — 적재만 한다. 진입 판정(WallDetector/FSM)은 여전히 하지 않는다:
          호가 잔량 이력은 어떤 공급자로도 과거 검증이 불가능하고
          (detect_multiplier 등은 도입 이후 한 번도 튜닝된 적 없는 placeholder),
          FSM 자체가 지연의 큰 축이었기 때문(413630 사례: FSM에서만 19분).
        """
        code = parsed_orderbook.get("stock_code")
        if not code:
            return
        if not (self.phase1b and getattr(self.phase1b, "orderbook", None)):
            return
        if not self.phase1b.is_watching(code):
            return
        try:
            self.phase1b.orderbook.update(code, parsed_orderbook, now=now)
        except Exception:
            logger.exception("[%s] 호가 적재 실패", code)

        # [보류] 1B 매도벽 FSM 트리거 — PHASE1B_ENABLED로 복구 가능
        # if PHASE1B_ENABLED:
        #     self.phase1b.wall_detector.on_orderbook(code, now=now)
        #     state = self.phase1b.evaluator.evaluate(code, now=now)
        #     if state == ChemulState.READY_TO_BUY:
        #         self._try_phase1b_buy(code, now)

    def _try_phase1b_buy(self, stock_code: str, now: float = None):
        """FSM이 READY_TO_BUY 도달 → 즉시 매수하지 않고 '반등확증' 대기 등록.
        (2026-07-30 변경, 근거는 PHASE1B_CONFIRM_WAIT 상수 주석 참고)

        [2026-08-01] 1B 비활성화 — 호출부(on_trade)가 이미 막고 있지만,
        나중에 다른 경로가 생겨도 새는 일이 없도록 여기서도 이중 가드한다."""
        if not PHASE1B_ENABLED:
            return
        if stock_code in self.holdings or stock_code in self.pending:
            return
        if stock_code in self._1b_confirm:
            return  # 이미 확증 대기 중
        if not self.can_buy_phase1b():
            logger.info("[%s] Phase 1B READY but 슬롯 부족", stock_code)
            return
        blocked, reason = self._is_rebuy_blocked(stock_code)
        if blocked:
            logger.info("[%s] Phase 1B 매수 차단: %s", stock_code, reason)
            return

        # 확증 기준선 = 신호 시점 1분봉의 고가("떨어진 자리")
        try:
            candles = self.api.get_minute_candles(stock_code, interval=1, count=2)
        except Exception as e:
            logger.warning("[%s] 1B 확증 기준선 조회 실패 → 매수 보류: %s", stock_code, e)
            return
        if not candles:
            logger.warning("[%s] 1B 확증 기준선 없음(분봉 0개) → 매수 보류", stock_code)
            return
        ref_high = float(candles[0].get("high") or 0)
        if ref_high <= 0:
            logger.warning("[%s] 1B 확증 기준선 이상(high=%s) → 매수 보류", stock_code, ref_high)
            return

        now_dt = self._now()
        self._1b_confirm[stock_code] = {
            "ref_high": ref_high,
            "since": now_dt,
            "last_check": now_dt,
        }
        logger.info(
            "[%s] 1B 반등확증 대기 시작 — 기준선(신호봉 고가) %s원 종가돌파 필요, %.0f분 내",
            stock_code, f"{ref_high:,.0f}", PHASE1B_CONFIRM_WAIT.total_seconds() / 60,
        )

    def _check_1b_confirmations(self):
        """반등확증 대기 종목을 '완성된 1분봉 종가'로 확인 (2026-07-30).
        tick()에서 주기 호출 — FSM이 READY 상태를 벗어나도 계속 추적되어야 하므로
        _try_phase1b_buy(틱 콜백)가 아니라 여기서 확인한다.

        [2026-08-01] 1B 비활성화 — tick()이 이미 막고 있지만 이중 가드."""
        if not PHASE1B_ENABLED:
            return
        if not self._1b_confirm:
            return
        now_dt = self._now()
        for code in list(self._1b_confirm.keys()):
            st = self._1b_confirm[code]

            if code in self.holdings or code in self.pending:
                self._1b_confirm.pop(code, None)
                continue

            if now_dt - st["since"] >= PHASE1B_CONFIRM_WAIT:
                self._1b_confirm.pop(code, None)
                logger.info(
                    "[%s] 1B 반등확증 실패 — %.0f분 내 기준선 %s원 미돌파, 매수 포기",
                    code, PHASE1B_CONFIRM_WAIT.total_seconds() / 60,
                    f"{st['ref_high']:,.0f}",
                )
                continue

            # 슬리피지 축소(2026-07-31) — 무료인 실시간 체결가로 먼저 기준선
            # 돌파 여부를 본다. 아직 안 넘었으면 REST 호출 자체를 생략(기존:
            # 넘었든 안 넘었든 55초마다 무조건 폴링 -> 429 예산도 오히려 아낌).
            # tick 데이터가 없으면(WS 순간 끊김 등) 기존 55초 폴링으로 폴백
            # (fail-safe — 확증 규칙 자체는 그대로, 감지 경로만 이원화).
            tick_price = (
                self.phase1b.trade_flow.get_latest_price(code) if self.phase1b else None
            )
            if PHASE1B_CONFIRM_TICK_PRECHECK and tick_price is not None:
                if tick_price <= st["ref_high"]:
                    continue
                gap = PHASE1B_CONFIRM_REST_GAP_SEC
            else:
                gap = PHASE1B_CONFIRM_CHECK_SEC

            if (now_dt - st["last_check"]).total_seconds() < gap:
                continue
            st["last_check"] = now_dt

            try:
                candles = self.api.get_minute_candles(code, interval=1, count=2)
            except Exception as e:
                logger.warning("[%s] 1B 확증 확인 실패(다음 주기 재시도): %s", code, e)
                continue
            if not candles or len(candles) < 2:
                continue

            # [0]은 진행 중인 봉이라 종가가 확정 안 됨 → [1](마지막 완성봉) 사용
            closed = candles[1]
            close_px = float(closed.get("close") or 0)
            if close_px <= st["ref_high"]:
                continue

            if not self.can_buy_phase1b():
                logger.info("[%s] 1B 확증됐으나 슬롯 부족 — 대기 유지", code)
                continue
            blocked, reason = self._is_rebuy_blocked(code)
            if blocked:
                self._1b_confirm.pop(code, None)
                logger.info("[%s] 1B 확증됐으나 매수 차단: %s", code, reason)
                continue

            current_price = (
                self.phase1b.trade_flow.get_latest_price(code) if self.phase1b else None
            ) or close_px
            self._1b_confirm.pop(code, None)
            logger.info(
                "[%s] 1B 반등확증 성립 — 완성봉 종가 %s원 > 기준선 %s원 (%.0f초 대기)",
                code, f"{close_px:,.0f}", f"{st['ref_high']:,.0f}",
                (now_dt - st["since"]).total_seconds(),
            )
            stock_name = self._stock_names.get(code, code)
            info = {"current_price": current_price, "volume_ratio": 0.0}
            self._execute_buy(code, stock_name, phase=1, info=info, sub_strategy="1B")
            if self.phase1b:
                self.phase1b.stop_watching(code)

    # ========================================
    # 진입 평가 (점수 기반 — scoring.py 위임)
    # ========================================
    def evaluate_phase1(self, candles, stock_code):
        return score_phase1(
            candles,
            self._volume_ratio(candles),
            self._current_strength(stock_code),
            self.score_cfg,
        )

    # ==========================================
    # 지수 방어막 로직
    # ==========================================
    def _get_market_defense_mode(self) -> str:
        """
        지수 방어 모드 확인 (코스피+코스닥 중 더 나쁜 쪽 기준, worst-of-two)
        반환: "NORMAL" (-3% 이내), "CAUTION" (-3% ~ -5%), "HALT" (-5% 초과)
        모드가 바뀔 때만 텔레그램 알림 (매 호출마다 알리면 스팸이라 전환 시점에만).
        """
        if not MARKET_DEFENSE_ENABLED:
            return "NORMAL"

        self._refresh_market_rates()
        worst = min(self._kospi_rate, self._kosdaq_rate)

        if worst <= -5.0:
            mode = "HALT"
        elif worst <= -3.0:
            mode = "CAUTION"
        else:
            mode = "NORMAL"

        if mode != self._last_market_mode:
            rate_str = f"코스피 {self._kospi_rate:+.2f}% / 코스닥 {self._kosdaq_rate:+.2f}%"
            if mode == "HALT":
                logger.warning(f"🛑 마켓 크래시 발동! {rate_str} - 전면 매매 중단")
                _notify(f"🛑 지수 급락으로 신규매수 전면 차단\n{rate_str}\n(보유분 청산은 계속 작동)")
            elif mode == "CAUTION":
                logger.info(f"⚠️ 지수 경보 발동! {rate_str} - 강세 조건 강화")
                _notify(f"⚠️ 지수 경보 — 진입조건 강화\n{rate_str}")
            else:
                logger.info(f"✅ 지수 방어 모드 해제 (NORMAL 복귀) {rate_str}")
                _notify(f"✅ 지수 정상화 — 방어 모드 해제\n{rate_str}")
            self._last_market_mode = mode

        return mode

    def _is_severe_crash(self) -> bool:
        """지수(코스피/코스닥 중 더 나쁜 쪽)가 SEVERE_CRASH_THRESHOLD 이하로
        급락했는지 여부. MARKET_DEFENSE_ENABLED와 무관하게 항상 동작 — 별개의
        독립 안전장치(2026-07-28). 모드 전환 시에만 텔레그램 알림."""
        self._refresh_market_rates()
        worst = min(self._kospi_rate, self._kosdaq_rate)
        is_crash = worst <= SEVERE_CRASH_THRESHOLD

        if is_crash != self._last_severe_crash_state:
            rate_str = f"코스피 {self._kospi_rate:+.2f}% / 코스닥 {self._kosdaq_rate:+.2f}%"
            if is_crash:
                logger.warning(f"🔻 지수 급락 대응 모드 진입! {rate_str}")
                _notify(
                    f"🔻 지수 급락 대응 모드 진입\n{rate_str}\n"
                    f"익절 flat {SEVERE_CRASH_TAKE_PROFIT*100:.1f}%로 통일(트레일링 해제, 손절 -3% 유지)\n"
                    f"{SEVERE_CRASH_ENTRY_CUTOFF.strftime('%H:%M')} 이후 신규매수 중단 (보유분은 수동판단)"
                )
            else:
                logger.info(f"✅ 지수 급락 대응 모드 해제 {rate_str}")
                _notify(f"✅ 지수 정상화 — 급락 대응 모드 해제\n{rate_str}")
            self._last_severe_crash_state = is_crash

        return is_crash

    def evaluate_1a_leading_strength(
        self, stock_code: str, current_price: float,
        open_price: float = 0.0, cond_name: str = "",
    ) -> tuple[bool, dict]:
        """1A 즉시진입 평가 — 체결강도(3초) + 대량체결 버스트.

        [2026-08-01 개편, 사용자 지정] 게이트 순서(위에서 걸리면 즉시 탈락):
          0) 감시 시작(체결틱/호가 수집) — 최초 호출부터 데이터를 모은다
          1) 주도주상위 한정, **당일 시가** 대비 +5% 이상이면 매수 보류
          2) 지수 HALT 차단
          3) 대량체결 버스트: 최근 3초에
               3천만원+ 단일체결 3건 이상  OR  1억원+ 단일체결 1건 이상
          4) 체결틱 3개 이상 (없으면 강도 '판단 불가'로 탈락 — 중립값 통과 방지)
          5) 3초 가중 체결강도 >= 100 x 동적배수(CAUTION/COLD일 때만 상향)
        통과 시 info에 score/score_threshold(=강도/임계값)를 실어 확장 슬롯과
        슬롯 교체가 다른 전략과 같은 축(컷라인 대비 비율)에서 비교되게 한다.

        ── 아래는 2026-07-31 도입 당시 배경(유지) ────────────────────

        도입 당시엔 주도주상위 소스 전용이었으나, 같은 논리(조건검색식이
        이미 강한 종목만 걸러 넘겨주므로 우리 쪽 추가 필터가 중복검증으로
        지연만 유발한다)를 돌파자동매매용까지 확장해 이제 소스 구분 없이
        1A 전체가 이 경로 하나로 통일됨(눌림목자동 단독 소스만 예외, Pullback
        직행). evaluate_new_intensity_strategy(거래량증가지속+2분강도지속+
        점수)는 더 이상 호출되지 않지만 삭제하지 않고 아래에 그대로 둠.

        배경: 413630 실사례 — 조건검색 편입 처리는 34초로 빨랐지만, 이후
        1A/Pullback/1L 3개 전략의 자체 게이트에 20분(09:23~09:43)이 걸려서야
        1B 경로로 겨우 매수됐다(그마저 로컬 눌림패턴이 안 맞아 1A/Pullback은
        끝내 통과 못 함).

        그래서 이 경로는 체결강도 하나만, 짧게(1분) 확인해서 즉시매수한다:
          - 거래량증가지속 제외: 시가부터 수직급등하는 종목이 많아 "3봉
            연속 증가"라는 완만한 패턴 자체가 안 맞는 경우가 많음.
          - 테마 요구사항 제외: 테마 리더십 축은 1L이 담당하던 것 — 1L을
            아예 주석처리했으므로(on_trade 참고) 이제 중복 우려 자체가
            없어짐, 순수 체결강도 하나로 판단.
          - 체결강도 가산점(구 intensity_bonus, 점수에 얹던 방식) 제거: 점수
            시스템 자체를 안 쓰므로 얹을 자리가 없음 — 대신 `_execute_buy`가
            sub_strategy 무관 항상 `entry_strength`를 기록하고, 기존 동적캡
            (on_price_update의 TP_UPGRADE_TRIGGER, _update_dynamic_caps의
            강도+거래량 동시하락 즉시매도)이 1L만 제외하고 전부에 이미 적용
            되므로 새 코드 없이 그대로 이관된다.

        phase1b 감시를 여기서 직접 켠다 — 기존 1A는 Pullback 블록이 나중에
        start_watching을 걸어줘야만 체결 틱이 쌓이는 구조적 공백이 있어서
        (최초 평가는 틱 0개라 is_intensity_sustained가 항상 False) 최초
        편입 시점엔 통과가 원천 불가능했다(오늘 세션에서 발견). 여기서는
        그 공백 없이 최초 호출부터 바로 틱을 모으기 시작한다."""
        if self.phase1b and not self.phase1b.is_watching(stock_code):
            self.phase1b.start_watching(stock_code)

        # 주도주상위 시가대비 급등 매수보류 (2026-07-31 사용자 지정) — 개장
        # 직후 시가 대비 이미 가파르게 오른 종목은 눌림(되돌림) 가능성이 커서
        # 보수적으로 접근한다는 취지. 주도주상위 소스에만 적용(사용자가 그렇게
        # 범위를 지정) — 감시(start_watching)는 그대로 유지해서 나중에 되돌림
        # 이후 재평가될 기회는 남겨둔다(watchlist_reentry가 계속 재시도).
        if "주도주상위" in cond_name and open_price > 0:
            surge_from_open = (current_price - open_price) / open_price * 100
            if surge_from_open >= PHASE1A_LEADING_OPEN_SURGE_CAP:
                return False, {
                    "current_price": current_price,
                    "reason": (
                        f"주도주상위 시가대비 +{surge_from_open:.1f}% "
                        f">= {PHASE1A_LEADING_OPEN_SURGE_CAP:.0f}% — 눌림 가능성으로 매수 보류"
                    ),
                }

        market_mode = self._get_market_defense_mode()
        if market_mode == "HALT":
            return False, {
                "current_price": current_price,
                "reason": "지수 -5% 초과로 인한 전면 매매 중단",
            }

        if not (self.phase1b and self.phase1b.trade_flow):
            return False, {
                "current_price": current_price,
                "reason": "체결강도 데이터 소스 없음(phase1b 미연결)",
            }

        # ── 대량체결 버스트 필터 (2026-08-01 사용자 지정) ──────────────
        # 기존 "60초 누적 거래대금 3천만원"을 대체. 누적은 잔챙이 체결이 길게
        # 쌓여도 채워져서 "지금 큰 손이 때리는가"를 구분 못 했다. 3초 창에서
        # 아래 둘 중 하나(OR)를 요구한다:
        #   ① 3천만원 이상 단일 체결이 3건 이상
        #   ② 1억원 이상 단일 체결이 1건이라도
        # 예외가 나도 매수로 새지 않도록 실패 시 0으로 두고 그대로 탈락시킨다.
        tf = self.phase1b.trade_flow
        try:
            burst_count = tf.count_large_trades(
                stock_code,
                window_sec=PHASE1A_LEADING_SUSTAIN_SEC,
                min_value=PHASE1A_BURST_TRADE_VALUE,
            )
            max_single = tf.max_single_trade_value(
                stock_code, window_sec=PHASE1A_LEADING_SUSTAIN_SEC
            )
            trade_value = tf.get_trade_value(
                stock_code, window_sec=PHASE1A_LEADING_SUSTAIN_SEC
            )
        except Exception:
            logger.exception("[%s] 1A 대량체결 계산 실패 — 매수 보류", stock_code)
            burst_count, max_single, trade_value = 0, 0.0, 0.0

        burst_ok = burst_count >= PHASE1A_BURST_TRADE_COUNT
        single_ok = max_single >= PHASE1A_SINGLE_TRADE_VALUE
        if not (burst_ok or single_ok):
            return False, {
                "current_price": current_price,
                "trade_value": trade_value,
                "burst_count": burst_count,
                "max_single_trade": max_single,
                "reason": (
                    f"대량체결 부족 (최근 {PHASE1A_LEADING_SUSTAIN_SEC}초: "
                    f"{PHASE1A_BURST_TRADE_VALUE/10000:,.0f}만원+ 체결 {burst_count}건"
                    f"/{PHASE1A_BURST_TRADE_COUNT}건, 최대단일 {max_single/10000:,.0f}만원"
                    f"/{PHASE1A_SINGLE_TRADE_VALUE/10000:,.0f}만원)"
                ),
            }

        # ── 체결강도 (3초 윈도우) ──────────────────────────────────
        # 틱 수를 직접 확인한 뒤 강도를 본다. compute_strength는 틱이 부족하면
        # 중립값(100.0)을 반환하는데 임계값도 100이라 "100 < 100 = False"로
        # **통과**해버린다 — 데이터가 없을수록 쉽게 뚫리는 구조라, 여기서는
        # 그 우연에 기대지 않고 틱 부족을 명시적으로 탈락시킨다.
        try:
            tick_n = tf.tick_count(stock_code, window_sec=PHASE1A_LEADING_SUSTAIN_SEC)
        except Exception:
            tick_n = 0
        if tick_n < PHASE1A_STRENGTH_MIN_TICKS:
            return False, {
                "current_price": current_price,
                "burst_count": burst_count,
                "max_single_trade": max_single,
                "reason": (
                    f"체결틱 부족 (최근 {PHASE1A_LEADING_SUSTAIN_SEC}초 {tick_n}틱 "
                    f"< 최소 {PHASE1A_STRENGTH_MIN_TICKS}틱) — 강도 판단 불가"
                ),
            }

        try:
            current_strength = tf.compute_strength(
                stock_code,
                window_sec=PHASE1A_LEADING_SUSTAIN_SEC,
                min_ticks=PHASE1A_STRENGTH_MIN_TICKS,
            )
        except Exception:
            logger.exception("[%s] 1A 체결강도 계산 실패 — 매수 보류", stock_code)
            current_strength = 0.0

        # 동적 강도 임계값 (2026-07-31 사용자 지정) — 고정 100은 "수치 호환성이
        # 안 맞을 때"(시장 전체가 흔들리거나 1A 자체가 오늘 잘 안 될 때) 그대로
        # 두면 너무 헐거워질 수 있어서, 새로 만들지 않고 이미 검증/구축된 두
        # 신호를 max()로 묶어 필요할 때만 위로 올린다(내리는 방향은 없음 —
        # 100을 바닥으로 고정, 진입을 원래보다 쉽게 만들 근거는 없다고 판단):
        #   ① 지수 방어 CAUTION — 시장 전체가 불안하면 "강해 보이는" 종목도
        #      신뢰도가 떨어진다는 게 옛 1A 점수시스템(PHASE1A_SCORE_CAUTION_BONUS)
        #      때부터의 전제. 같은 논리를 강도 임계값에도 그대로 적용.
        #   ② core/strategy_performance.py의 cutline_multiplier("1A") — 이미
        #      점수 컷라인용으로 만들어둔 축소추정 기반 HOT/COLD 조정을 재사용.
        #      COLD(오늘 1A 성과 나쁨)면 배수>1이 나와 임계값이 오르고, HOT이면
        #      <1이 나오지만 max(1.0, ...)로 묶어서 100 밑으로는 안 내려간다.
        #   ③ [제안 B, 2026-08-01] 같은 축소추정을 **조건검색식별로도** 적용.
        #      오늘 그 검색식이 물어다 준 종목들의 성과가 나쁘면(COLD) 배수가
        #      1을 넘어 임계값이 올라가고, 좋으면 1 미만이 나오지만 아래 max()가
        #      100을 바닥으로 잡아 진입이 원래보다 쉬워지지는 않는다.
        #      우선순위(줄 세우기)가 아니라 문턱 조정이라 진입 지연이 0이다.
        perf_mult = self.perf.cutline_multiplier("1A")
        cond_mult = self.perf.cutline_multiplier(self.cond_perf_key(cond_name))
        caution_mult = PHASE1A_LEADING_CAUTION_MULTIPLIER if market_mode == "CAUTION" else 1.0
        strength_mult = max(1.0, perf_mult, cond_mult, caution_mult)
        effective_strength_min = PHASE1A_LEADING_STRENGTH_MIN * strength_mult

        # 3초 창 전체를 하나의 시간가중 윈도우로 평가한다 (2026-08-01).
        # 기존 is_intensity_sustained(60초 구간을 10초 간격 샘플링)는 창이
        # 3초로 줄면 샘플이 1개뿐이라 의미가 없고, 무엇보다 틱이 없는 구간에
        # 중립값(100)을 채워 임계값 100을 그냥 통과시키는 성질이 있었다.
        # 여기서는 위에서 틱 수를 이미 확인했으므로 강도 비교만 하면 된다 —
        # 3초 가중 윈도우라 순간 스파이크 1틱으로는 통과할 수 없다.
        if current_strength < effective_strength_min:
            return False, {
                "current_price": current_price,
                "current_strength": current_strength,
                "strength_threshold": effective_strength_min,
                "score": current_strength,
                "score_threshold": effective_strength_min,
                "burst_count": burst_count,
                "max_single_trade": max_single,
                "reason": (
                    f"체결강도 미달 ({current_strength:.0f} < {effective_strength_min:.0f}"
                    f" = 기본 {PHASE1A_LEADING_STRENGTH_MIN:.0f} x{strength_mult:.2f}, "
                    f"{PHASE1A_LEADING_SUSTAIN_SEC}초 {tick_n}틱)"
                ),
            }

        trigger = (
            f"대량체결 {burst_count}건" if burst_ok
            else f"단일체결 {max_single/100_000_000:.2f}억"
        )
        return True, {
            "current_price": current_price,
            "current_strength": current_strength,
            "strength_threshold": effective_strength_min,
            # score/score_threshold는 확장슬롯(_can_use_expansion_slot)·슬롯교체가
            # 전략 공통으로 읽는 필드다 (2026-08-01 추가). 이게 없어서 1A는
            # 확장 슬롯을 영구히 못 쓰고 슬롯교체 대체후보 자격도 없었다.
            # 1A엔 점수 개념이 없으므로 체결강도/임계값을 그대로 싣는다 —
            # 스케일이 전략마다 달라도 '컷라인 대비 비율'로 비교하므로 형평이 맞는다.
            "score": current_strength,
            "score_threshold": effective_strength_min,
            "burst_count": burst_count,
            "max_single_trade": max_single,
            "trade_value": trade_value,
            "entry_mode": "1a_leading_strength",
            "reason": (
                f"1A 즉시진입 (강도 {current_strength:.0f}>={effective_strength_min:.0f} "
                f"/{PHASE1A_LEADING_SUSTAIN_SEC}초 {tick_n}틱, {trigger})"
            ),
        }

    def evaluate_new_intensity_strategy(
        self,
        stock_code: str,
        candles: list,
        current_price: int,
        open_price: int,
        now_t=None,
    ) -> tuple[bool, dict]:
        """체결강도 지속 & 거래량증가지속 기반의 1A 평가 로직 (지수 방어막 포함).
        10:30부터는 점수 커트라인을 상향(PHASE1A_SCORE_TIGHT)해서 더 빡빡하게.

        [주석으로 보류, 2026-07-31] 1A 라우팅이 evaluate_1a_leading_strength
        (체결강도 단독)로 전면 대체되면서 현재 어디서도 호출되지 않음. 삭제하지
        않고 남겨둠 — 심플화가 기대만큼 안 되면 _evaluate_1a_pullback_entry의
        라우팅을 이 함수 호출로 되돌리면 됨."""
        now = now_t if now_t is not None else self._now().time()

        # [최우선] 지수 방어막 확인
        market_mode = self._get_market_defense_mode()
        if market_mode == "HALT":
            return False, {"reason": "지수 -5% 초과로 인한 전면 매매 중단", "score": 0.0}

        # 1. 거래량증가지속 필터 (30봉신고가 대신, 2026-07-27 교체)
        # 개장 초반(대략 09:01~09:04)엔 오늘 분봉이 streak+1(4)개도 안 쌓여서
        # _get_merged_candles가 전일 마지막 분봉으로 채워 넣는데, 그러면 "어제
        # 장마감 무렵 거래량 vs 오늘 개장 1~2분"이라는 의미없는 비교가 되어
        # 매번 탈락함 (2026-07-29 실전 확인 — 오늘 조건검색 21종목 전부 이
        # 이유로 탈락, 1A가 하루 종일 0건). 전일 데이터는 섞지 않고 오늘
        # 분봉만으로 판단, 아직 부족하면 탈락이 아니라 보류(다음 재평가 때
        # 다시 시도 — watchlist_reentry가 슬롯 빌 때마다 재호출함).
        today_str = self._now().strftime("%Y%m%d")
        today_only = [c for c in candles if c.get("time_str", "").startswith(today_str)]
        if len(today_only) < 4:
            return False, {"reason": "오늘 분봉 부족(개장 초반) - 거래량 판정 보류", "score": 0.0}
        if not is_volume_increasing_streak(today_only):
            return False, {"reason": "거래량 증가 지속 아님", "score": 0.0}

        # 2. 시간대별 체결강도 지속 필터
        is_pass = False
        if now < PHASE1A_TIGHTEN_TIME:
            if not open_price or open_price <= 0:
                return False, {"reason": "시작가 없음", "score": 0.0}

            change_pct = (current_price - open_price) / open_price * 100

            if change_pct <= 5.0:
                is_pass = self.phase1b.trade_flow.is_intensity_sustained(stock_code, 95, 60)
            elif change_pct <= 8.0:
                is_pass = self.phase1b.trade_flow.is_intensity_sustained(stock_code, 120, 60)
            else:
                is_pass = self.phase1b.trade_flow.is_intensity_sustained(stock_code, 160, 60)
        else:
            is_pass = self.phase1b.trade_flow.is_intensity_sustained(stock_code, 105, 120)

        if not is_pass:
            return False, {"reason": "체결강도 지속 미달", "score": 0.0}

        # 3. 점수 계산 (기존 score 시스템 활용) — evaluate_phase1 자체의 ok는
        # 안 씀(2026-07-30 수정). self.score_cfg.threshold_ratio(0.75, 12점 만점
        # 9.0점)가 바로 아래 5번의 진짜 시간대별 커트라인(오전 6.5/10:30이후 8.5)
        # 보다 항상 더 높아서, ok로 먼저 거르면 "10:30부터 빡빡하게"라는 설계가
        # 하루 종일 죽은 코드가 됨(9.0을 넘겨야 여기까지 오는데 9.0은 6.5·8.5
        # 둘 다보다 크므로). base_score는 점수만 뽑아 쓰고 최종 판정은 5번의
        # required_score 하나로 통일.
        _, info = self.evaluate_phase1(candles, stock_code)

        base_score = info.get("score", 5.0)

        # 4. 체결강도 가산점 계산
        try:
            current_intensity = self.phase1b.trade_flow.compute_strength(stock_code, window_sec=10)
        except Exception:
            current_intensity = 100

        intensity_bonus = min(current_intensity / 100 * 1.0, 3.0)
        final_score = base_score + intensity_bonus

        # 5. 시간대(10:30 기준) + 지수 상태(CAUTION)에 따른 동적 커트라인
        base_required = (
            PHASE1A_SCORE_TIGHT if now >= PHASE1A_TIGHTEN_TIME else PHASE1A_SCORE_NORMAL
        )
        required_score = base_required + (
            PHASE1A_SCORE_CAUTION_BONUS if market_mode == "CAUTION" else 0.0
        )
        # 장중 전략 성과에 따른 자동 조정 (2026-07-31) — 오늘 1A가 잘 되고 있으면
        # 컷라인을 낮춰 슬롯을 더 가져가고, 안 되면 높여 스스로 물러난다.
        perf_mult = self.perf.cutline_multiplier("1A")
        required_score *= perf_mult

        # score_threshold를 함께 실어야 확장 슬롯 판정(_can_use_expansion_slot)이
        # 1A에도 적용된다 — 없으면 Pullback만 확장 슬롯을 쓸 수 있어 형평이 깨짐.
        if final_score >= required_score:
            return True, {
                "reason": f"1A 통과 (강도:{current_intensity:.0f}, 모드:{market_mode})",
                "score": final_score,
                "score_threshold": required_score,
                "perf_multiplier": perf_mult,
            }
        else:
            return False, {
                "reason": f"최종 점수 부족 ({final_score:.1f}/{required_score:.1f})",
                "score": final_score,
                "score_threshold": required_score,
                "perf_multiplier": perf_mult,
            }

    def evaluate_pullback(self, candles, stock_code, obv_mom: float = 0.0,
                          skip_setup_check: bool = False):
        # 주의: 인자명 obv_mom은 일부러 obv_momentum(모듈 임포트된 함수명)과
        # 다르게 지음 — 같은 이름을 지역변수/인자로 쓰면 함수 스코프 내에서
        # obv_momentum이 지역변수로 취급돼 호출부(_evaluate_1a_pullback_entry)의
        # obv_momentum(...) 호출이 UnboundLocalError로 깨짐.
        cfg = self._adjusted_cfg(self.pullback_score_cfg)
        # 장중 전략 성과 반영 (2026-07-31) — Pullback 전용 컷라인(threshold_abs)에
        # 배수를 적용. threshold_abs를 안 쓰는 설정이면 비율 쪽에 적용한다.
        perf_mult = self.perf.cutline_multiplier("1A_눌림")
        if perf_mult != 1.0:
            if cfg.threshold_abs is not None:
                cfg = _dc_replace(cfg, threshold_abs=cfg.threshold_abs * perf_mult)
            else:
                cfg = _dc_replace(cfg, threshold_ratio=cfg.threshold_ratio * perf_mult)
        return score_pullback(
            candles,
            self._volume_ratio(candles),
            self._current_strength(stock_code),
            cfg,
            obv_momentum=obv_mom,
            skip_setup_check=skip_setup_check,
        )

    def _apply_vwap_filter(
        self, stock_code: str, strat_name: str, current_price: float, info: dict,
        today_candles: list | None = None,
    ) -> bool:
        """눌림목(pullback) 전용 VWAP AND 필터. 다른 전략은 항상 통과.
        반환 True = 매수 진행, False = VWAP 탈락으로 매수 보류.
        주의: VWAP은 당일 09:00~현재 누적이라야 정확함. 전일 데이터가
        섞이는 _get_merged_candles는 절대 쓰지 않고, get_minute_candles를
        직접 호출해서 당일 데이터만 사용함.
        today_candles: 호출부가 이미 당일 전용 캔들을 갖고 있으면(예: OBV
        점수 계산에 이미 씀) 넘겨서 REST 재호출 방지, 없으면 새로 조회."""
        if strat_name != "pullback":
            return True
        try:
            vwap_candles = (
                today_candles if today_candles is not None
                else self.api.get_minute_candles(stock_code, interval=1, count=400)
            )
            vwap = calc_vwap(vwap_candles)
        except Exception as e:
            logger.warning("[%s] VWAP 계산 실패, 필터 스킵: %s", stock_code, e)
            return True  # 계산 실패 시 필터로 막지 않음 (기존 로직 유지)

        volume_ratio = 1.0
        try:
            volume_ratio = self._volume_ratio(vwap_candles)
        except Exception:
            pass

        result = self.vwap_strategy.evaluate(
            {
                "price": current_price,
                "vwap": vwap,
                "candles": vwap_candles,
                "volume_ratio": volume_ratio,
            }
        )
        info["vwap"] = vwap
        info["vwap_score"] = result["score"]
        info["vwap_confidence"] = result.get("confidence")
        info["vwap_gates"] = result.get("gates")

        if not result["bullish"]:
            logger.info(
                "[%s] pullback 조건 OK but VWAP 필터 탈락 "
                "(price=%.0f vwap=%.0f gap=%.2f%% score=%s conf=%s gates=%s)",
                stock_code,
                current_price,
                vwap,
                result.get("gap_pct", 0.0),
                result["score"],
                result.get("confidence"),
                result.get("gates"),
            )
            return False

        return True

    def _current_strength(self, stock_code):
        # 체결강도 조회. phase1b 없거나 실패/데이터 없으면 중립값 100 반환.
        if stock_code and self.phase1b and getattr(self.phase1b, "trade_flow", None):
            try:
                return self.phase1b.trade_flow.compute_strength(
                    stock_code, window_sec=10
                )
            except Exception:
                pass
        return 100.0

    @staticmethod
    def _volume_ratio(candles: list[dict]) -> float:
        cur_vol = candles[0]["volume"]
        prev = candles[1 : 1 + VOLUME_LOOKBACK]
        if not prev:
            return 0.0
        avg = sum(c["volume"] for c in prev) / len(prev)
        return cur_vol / avg if avg > 0 else 0.0

        # ========================================

    # 매수 실행
    # ========================================
    @staticmethod
    def tier_size_multiplier(tier: float) -> float:
        """tier -> 매수금액 배수 (2026-08-01, 제안 C).

        tier 1.0 이하 = 1.0배(기존과 동일), tier PHASE1A_SIZE_TIER_FULL 이상 =
        PHASE1A_SIZE_MAX_MULT배. 그 사이는 선형.

        **위로만 움직인다.** 아래로도 열면 평범한 후보의 매수 수량이 0주가 되어
        조용히 매매를 잃는 부작용이 생기고, 지금은 자본의 2.4%만 쓰는 단계라
        줄일 이유가 없다. 상한이 고정이라 tier가 아무리 커도(가속도가 폭발해도)
        금액이 폭주하지 않는다 — 이 함수가 유일한 확대 경로다.
        """
        try:
            t = float(tier)
        except (TypeError, ValueError):
            return 1.0
        if t <= 1.0:
            return 1.0
        span = max(PHASE1A_SIZE_TIER_FULL - 1.0, 1e-9)
        ratio = min((t - 1.0) / span, 1.0)
        return 1.0 + ratio * (PHASE1A_SIZE_MAX_MULT - 1.0)

    def _resolve_position_amount(
        self, stock_code: str, sub_strategy: str, tier: float = 0.0
    ) -> tuple[int, Optional[dict]]:
        base = POSITION_AMOUNT
        opt_info = None
        if self.optimizer is not None:
            try:
                info = self.optimizer.calculate_position_amount(stock_code, sub_strategy)
                amount = int(info.get("amount", 0))
                if amount > 0:
                    base, opt_info = amount, info
            except Exception:
                logger.exception(f"[{stock_code}] 비중 계산 실패, fallback")

        # [제안 C] tier 가중 — 같은 슬롯 하나라도 신호가 강한 자리에 더 태운다.
        tier_mult = self.tier_size_multiplier(tier)
        if tier_mult > 1.0:
            boosted = int(base * tier_mult)
            logger.info(
                "[%s] tier 가중 매수금액: %s원 x%.2f -> %s원 (tier %.2f)",
                stock_code, f"{base:,}", tier_mult, f"{boosted:,}", tier,
            )
            base = boosted
            if opt_info is not None:
                # 알림/로그에 최종 비중이 그대로 드러나도록 합성 배수를 실어준다.
                opt_info = dict(opt_info)
                opt_info["final_weight"] = opt_info.get("final_weight", 1.0) * tier_mult
        return base, opt_info

    def _get_opening_price(self, stock_code: str) -> Optional[float]:
        """당일 시가 조회 — on_condition_hit에서 이미 캐시했으면 재사용,
        없으면(1B/1L처럼 조건검색을 거치지 않고 실시간 틱으로 바로 들어온 경우
        캐시가 없을 수 있음) REST로 1회 조회 후 캐시. 실패 시 None(상한 체크 스킵)."""
        cached = self._opening_prices.get(stock_code)
        if cached:
            return cached
        try:
            candles = self.api.get_minute_candles(stock_code, interval=1, count=400)
            today = self._now().strftime("%Y%m%d")
            today_candles = [c for c in candles if c.get("time_str", "").startswith(today)]
            if not today_candles:
                return None
            open_price = float(min(today_candles, key=lambda c: c["time_str"])["open"])
            if open_price > 0:
                self._opening_prices[stock_code] = open_price
                return open_price
        except Exception as e:
            logger.warning("[%s] 시가 조회 실패, 등락률 상한 체크 스킵: %s", stock_code, e)
        return None

    def _get_prev_close(self, stock_code: str, current_price: float) -> Optional[float]:
        """전일종가 조회/캐시 (2026-07-31, 매수 등락률 상한 체크용).

        전일종가는 하루 내내 불변이므로 종목당 1회만 REST 호출하고 캐시한다.
        기존 get_stock_change_rate(ka10001, 이미 있는 메서드)는 등락률(%)만
        주므로, 지금 아는 current_price로 역산해서 전일종가 값 자체를 저장한다
        — 그래야 이후엔 API 재호출 없이 실시간 current_price만으로 등락률을
        즉시 계산할 수 있다(분봉 400개를 통째로 받아오던 예전 방식보다 REST
        부담이 훨씬 적음 — 이 계정은 이미 429가 하루 2천 건대로 포화 상태)."""
        cached = self._prev_closes.get(stock_code)
        if cached:
            return cached
        try:
            change_pct = self.api.get_stock_change_rate(stock_code)
            if not current_price or change_pct is None:
                return None
            prev_close = current_price / (1 + change_pct / 100)
            if prev_close > 0:
                self._prev_closes[stock_code] = prev_close
                return prev_close
        except Exception as e:
            logger.warning("[%s] 전일종가 조회 실패, 등락률 상한 체크 스킵: %s", stock_code, e)
        return None

    def _resolve_order_style(self, stock_code: str, current_price: float) -> tuple[str, int, str]:
        """호가창 두께로 지정가/시장가를 고른다 (2026-08-01 사용자 지정).

        매도 1~PHASE1A_ASK_DEPTH_LEVELS 호가의 잔량 '금액'을 보고:
          >= PHASE1A_ASK_DEPTH_MIN : 받아줄 물량이 충분 -> 시장가 (즉시체결)
          <  PHASE1A_ASK_DEPTH_MIN : 텅 빈 호가창 -> 지정가 (훑고 올라가는 것 방지)
          호가 스냅샷 없음/예외    : 지정가 (보수적 기본값)

        반환: (style, ref_price, 사유문자열)
          ref_price = 시장가일 때 '예상 체결가'로 쓸 매도1호가(없으면 0 -> 호출부가
          현재가로 대체). 지정가 경로에선 쓰이지 않는다.

        절대 예외를 밖으로 던지지 않는다 — 호가 판정 실패가 매수 자체를
        막거나 크래시로 번지면 안 되므로, 어떤 실패든 '지정가'로 안전하게 수렴한다.
        """
        try:
            ob = getattr(self.phase1b, "orderbook", None) if self.phase1b else None
            if ob is None:
                return "limit", 0, "호가 트래커 없음 -> 지정가"
            depth_value = ob.get_ask_depth_value(
                stock_code, levels=PHASE1A_ASK_DEPTH_LEVELS
            )
            if depth_value is None:
                return "limit", 0, "호가 스냅샷 없음 -> 지정가"
            ask1, _ = ob.get_top_ask(stock_code)
            ref = int(ask1) if ask1 else 0
            if depth_value >= PHASE1A_ASK_DEPTH_MIN:
                return "market", ref, (
                    f"매도1~{PHASE1A_ASK_DEPTH_LEVELS}호가 잔량 "
                    f"{depth_value/10000:,.0f}만원 >= {PHASE1A_ASK_DEPTH_MIN/10000:,.0f}만원 "
                    f"-> 시장가"
                )
            return "limit", ref, (
                f"매도1~{PHASE1A_ASK_DEPTH_LEVELS}호가 잔량 "
                f"{depth_value/10000:,.0f}만원 < {PHASE1A_ASK_DEPTH_MIN/10000:,.0f}만원 "
                f"(빈 호가창) -> 지정가"
            )
        except Exception:
            logger.exception("[%s] 호가 두께 판정 실패 -> 지정가로 진행", stock_code)
            return "limit", 0, "호가 판정 예외 -> 지정가"

    def _fresh_tick_price(self, stock_code: str, max_age_sec: float = 10.0):
        """최근 max_age_sec 안에 체결이 있었을 때만 최신 체결가를 반환.

        (2026-08-01) 매수 단가 기준을 분봉 종가(최대 1분 지연) 대신 실시간
        체결가로 올리기 위한 헬퍼. 오래된 틱을 '현재가'로 잘못 쓰지 않도록
        반드시 신선도를 함께 확인한다 — 그냥 get_latest_price만 쓰면 몇 분 전
        가격이 그대로 나올 수 있다(감시만 켜두고 체결이 끊긴 종목).
        """
        if not (self.phase1b and getattr(self.phase1b, "trade_flow", None)):
            return None
        try:
            if self.phase1b.trade_flow.tick_count(stock_code, max_age_sec) <= 0:
                return None
            px = self.phase1b.trade_flow.get_latest_price(stock_code)
            return px if px and px > 0 else None
        except Exception:
            return None

    def _execute_buy(self, stock_code, stock_name, phase, info, sub_strategy):
        current_price = info["current_price"]
        # 실시간 체결가가 있으면 그걸 기준가로 쓴다 (2026-08-01) — info의
        # current_price는 1분봉 종가라 최대 1분 지연이고, 그 값으로 수량/
        # 등락률상한/매수단가를 계산하면 빠르게 움직이는 종목에서 손익 판정이
        # 처음부터 어긋난다("매수가가 한참 위에서 된다"의 한 축).
        fresh_px = self._fresh_tick_price(stock_code)
        if fresh_px:
            if current_price and abs(fresh_px - current_price) / current_price > 0.10:
                # 10% 넘게 벌어지면 둘 중 하나가 이상한 값 — 보수적으로 분봉값 유지
                logger.warning(
                    "[%s] 실시간가(%s)와 분봉가(%s) 괴리 10%% 초과 — 분봉값 사용",
                    stock_code, f"{fresh_px:,}", f"{current_price:,}",
                )
            else:
                current_price = fresh_px

        if not current_price or current_price <= 0:
            logger.warning("[%s] %s 기준가 없음 -> 매수 취소", stock_code, stock_name)
            return

        if self._now().time() >= ENTRY_HARD_CUTOFF:
            logger.info(
                "[%s] %s 매수 차단: %s 이후 신규매수 하드 컷오프 [%s]",
                stock_code, stock_name, ENTRY_HARD_CUTOFF.strftime("%H:%M"), sub_strategy,
            )
            return

        if self._is_severe_crash() and self._now().time() >= SEVERE_CRASH_ENTRY_CUTOFF:
            logger.info(
                "[%s] %s 매수 차단: 지수 급락 대응 — %s 이후 신규매수 중단 [%s]",
                stock_code, stock_name, SEVERE_CRASH_ENTRY_CUTOFF.strftime("%H:%M"), sub_strategy,
            )
            return

        cond_name_now = self._cond_names.get(stock_code, "")

        # 눌림목 조건검색 종목은 Pullback 전략으로만 매수 (2026-08-01 사용자 지정).
        # 라우팅(_evaluate_1a_pullback_entry)에서 이미 배타적으로 갈리지만,
        # 다른 경로가 추가되거나 cond_name이 뒤늦게 병합돼도 절대 새지 않도록
        # 실제 주문 직전인 여기서 한 번 더 막는다(단일 차단 지점).
        if "눌림목자동" in cond_name_now and sub_strategy != "1A_눌림":
            logger.info(
                "[%s] %s 매수 차단: 눌림목자동 종목은 Pullback 전용 [요청 전략=%s]",
                stock_code, stock_name, sub_strategy,
            )
            self._note_reject(stock_code, "눌림목자동 종목은 Pullback 전용")
            return

        entry_cap = self._entry_change_cap(sub_strategy, cond_name_now)
        prev_close = self._get_prev_close(stock_code, current_price)
        if prev_close:
            change_pct = (current_price - prev_close) / prev_close * 100
            if change_pct > entry_cap:
                logger.info(
                    "[%s] %s 매수 차단: 전일종가대비 +%.1f%% (상한 +%.0f%%) [%s]",
                    stock_code, stock_name, change_pct, entry_cap, sub_strategy,
                )
                self._note_reject(
                    stock_code,
                    f"등락률 상한 초과 (전일종가대비 +{change_pct:.1f}% > +{entry_cap:.0f}%)",
                )
                return

        sc = info.get("score")
        if sc is not None:
            logger.info(
                "[%s] %s 매수평가 통과 | score=%.2f/%.2f | %s",
                stock_code,
                stock_name,
                sc,
                info.get("score_threshold", 0),
                info.get("score_breakdown", ""),
            )

        # tier는 매수금액 가중(제안 C)과 포지션 기록(사후 검증)에 둘 다 쓰이므로
        # 여기서 한 번만 계산해 재사용한다 — 메모리 연산이라 비용은 없지만
        # 두 번 부르면 값이 미세하게 달라져 로그와 기록이 어긋난다.
        entry_tier = self.candidate_tier(stock_code)
        position_amount, opt_info = self._resolve_position_amount(
            stock_code, sub_strategy, tier=entry_tier
        )
        quantity = int(position_amount // current_price)
        if quantity < 1:
            logger.warning("[%s] %s 수량 0 -> skip", stock_code, stock_name)
            return

        if opt_info:
            logger.info(
                "[%s] 동적 비중 %.2fx -> %s원",
                stock_code,
                opt_info.get("final_weight", 1.0),
                f"{position_amount:,}",
            )

        self.pending.add(stock_code)
        self._pending_strategy[stock_code] = sub_strategy
        try:
            ma_val = info.get("ma5") or 0
            if sub_strategy == "1B":
                entry_reason = f"1B 체결강도 FSM (현재가 {current_price:,})"
            elif sub_strategy == "1L":
                entry_reason = (
                    f"주도주 우선 진입 | 테마: {info.get('theme', '?')} "
                    f"| 체결강도 100이상 2분지속 (현재가 {current_price:,})"
                )
            elif sub_strategy == "1A_눌림":
                entry_reason = (
                    f"눌림목 반등 | MA5={ma_val:,.0f} "
                    f"| vol x{info.get('volume_ratio', 0):.2f} (현재가 {current_price:,})"
                )
            elif info.get("entry_mode") == "1a_leading_strength":
                # 1A - 체결강도 단독 즉시진입(2026-07-31, evaluate_1a_leading_strength)
                entry_reason = (
                    f"1A 체결강도 즉시진입(강도만) | 강도={info.get('current_strength', 0):.0f} "
                    f"(현재가 {current_price:,})"
                )
            else:
                # 1A (거래량증가지속+체결강도지속+점수, 주도주상위 이외 소스)
                entry_reason = (
                    f"1A 거래량증가지속+체결강도지속 | score={info.get('score', 0):.2f} "
                    f"| vol x{info.get('volume_ratio', 0):.2f} (현재가 {current_price:,})"
                )
            if opt_info:
                entry_reason += f" | 비중x{opt_info.get('final_weight', 1.0):.2f}"

            if "vwap" in info:
                vwap = info["vwap"]
                gap_pct = ((current_price - vwap) / vwap * 100) if vwap > 0 else 0.0
                entry_reason += f" | VWAP {vwap:,.0f} (gap {gap_pct:+.2f}%, {info.get('vwap_score', 0)}점)"
                conf = info.get("vwap_confidence")
                if conf is not None:
                    entry_reason += f" | conf {conf:.2f}"
                gates = info.get("vwap_gates")
                if gates:
                    gate_str = ",".join(
                        f"{k}{'O' if v else 'X'}" for k, v in gates.items()
                    )
                    entry_reason += f" | gates[{gate_str}]"

            # 조건검색식 이름 프리픽스 (2026-07-06)
            cond_name = self._cond_names.get(stock_code, "알수없음")
            display_reason = entry_reason  # 텔레그램용 — cond_name 프리픽스 전 원문
            entry_reason = f"[{cond_name}] {entry_reason}"

            # ── 하이브리드 주문 (2026-08-01 사용자 지정) ────────────────
            # 호가창이 두툼하면 시장가(즉시체결), 텅 비었으면 지정가.
            # 판정이 실패하면 무조건 지정가로 수렴하므로 여기서 예외로
            # 매수가 통째로 깨지는 일은 없다(_resolve_order_style 참고).
            order_style, ask1, style_reason = self._resolve_order_style(
                stock_code, current_price
            )
            logger.info("[%s] 주문방식 판정: %s", stock_code, style_reason)
            result = self.order_manager.buy(
                stock_code,
                quantity,
                price=0,
                order_style=order_style,
                ref_price=ask1 or int(current_price),
            )

            # 시장가 실패 시 지정가 1회 폴백 (2026-08-01).
            # 이 계정은 모의투자이고, 지금까지 실제로 낸 주문은 전부
            # trde_tp="0"(지정가)뿐이라 모의 서버가 trde_tp="3"(시장가)을
            # 받아주는지 실거래로 확인된 적이 없다. 새 주문 방식 때문에
            # 매수 자체를 놓치는 일이 없도록, 시장가가 거부되면 즉시
            # 검증된 지정가 경로로 한 번 더 시도한다.
            if order_style == "market" and (not result or not result.get("success")):
                logger.warning(
                    "[%s] 시장가 주문 실패(%s) -> 지정가로 1회 폴백",
                    stock_code, (result or {}).get("error", "unknown"),
                )
                order_style = "limit"
                result = self.order_manager.buy(
                    stock_code, quantity, price=0, order_style="limit",
                )

            if not result or not result.get("success"):
                err = (result or {}).get("error", "unknown")
                logger.error("[%s] 매수 실패: %s", stock_code, err)
                SystemEventRepository.log(
                    "ORDER_FAIL", f"BUY {stock_code}: {err}", "ERROR"
                )
                _notify(
                    f"매수 실패\n{stock_code} {stock_name}\n사유: {err}", target="order"
                )
                return

            # 체결 기준가 — 지정가면 주문가, 시장가면 매도1호가(예상 체결가).
            # order_manager가 실제로 쓴 값을 그대로 받아야 손절/익절 판정이
            # 실제 체결가와 최소한으로 어긋난다 (2026-08-01).
            fill_price = int(result.get("price") or current_price)
            if fill_price <= 0:
                fill_price = int(current_price)
            entry_reason += f" | {order_style}"
            # trades.entry_reason은 VARCHAR(255) — 넘치면 insert가 통째로 실패해
            # trade_id가 없어지고 매도 DB 갱신까지 끊긴다(2026-07-27 실제 사고).
            # 길이를 안전하게 잘라서 그 클래스의 사고를 원천 차단한다.
            entry_reason = entry_reason[:250]

            # 매수 주문은 이미 체결됨 — DB 기록이 실패해도 포지션 추적(self.holdings)은
            # 반드시 이어져야 하므로 insert_buy 예외가 위로 전파되지 않게 막는다.
            try:
                trade_id = TradeRepository.insert_buy(
                    stock_code=stock_code,
                    stock_name=stock_name,
                    buy_price=fill_price,
                    buy_quantity=quantity,
                    strategy_phase=phase,
                    sub_strategy=sub_strategy,
                    entry_reason=entry_reason,
                )
            except Exception as e:
                trade_id = None
                logger.exception("[%s] 매수 DB 기록 실패 (포지션은 정상 보유): %s", stock_code, e)
                SystemEventRepository.log(
                    "DB_LOG_FAIL", f"insert_buy {stock_code}: {e}", "ERROR"
                )

            strength_val = self._current_strength(stock_code)

            self.holdings[stock_code] = {
                "trade_id": trade_id,
                "buy_price": fill_price,
                "buy_quantity": quantity,
                "buy_time": self._now(),
                "stock_name": stock_name,
                "strategy_phase": phase,
                "sub_strategy": sub_strategy,
                "highest_price": fill_price,
                "lowest_price": fill_price,
                "ma20": None,
                "ma20_updated": None,
                "trigger": info.get("trigger"),
                "opening_price": info.get("opening_price", 0.0),
                "position_weight": (opt_info or {}).get("final_weight", 1.0),
                "warmup_until": self._now() + BUY_WARMUP,
                # entry_score도 _watch_scores와 같은 '컷라인 대비 비율' 스케일로
                # 통일 (2026-08-01) — 슬롯교체가 이 값과 후보 점수를 직접
                # 비교하는데, 한쪽만 원점수면 전략 간 비교가 무의미해진다.
                "entry_score": self._score_ratio(info),
                "entry_strength": strength_val,
                # 진입 시점 종목 tier — 사후 분석용(이 tier가 실제 성과를
                # 예측했는지 며칠 쌓아 검증할 것). 교체 판정은 '진입 tier'가
                # 아니라 항상 '현재 tier'로 한다.
                "entry_tier": entry_tier,
                # 조건검색식 성과 축 키 (제안 B) — 청산 시 이 키로 성과를 기록해야
                # "어느 검색식이 오늘 잘 물어다 주는가"가 축적된다.
                "cond_key": self.cond_perf_key(cond_name),
                "order_style": order_style,
            }

            self.sold_at.pop(stock_code, None)
            self._buy_success_count += 1
            self._last_buy_at = self._now()      # 진단용(매수 공백 감지)
            self._last_reject.pop(stock_code, None)
            # 당일 매수 횟수 누적 (익절 후 재매수 상한 판정용, 2026-07-30)
            self._buy_count_today[stock_code] = (
                self._buy_count_today.get(stock_code, 0) + 1
            )

            logger.info(
                "BUY [%s] %s %d주 @ %s원 (%s) = %s원 | 워밍업 %ds",
                stock_code,
                stock_name,
                quantity,
                f"{current_price:,}",
                sub_strategy,
                f"{current_price * quantity:,}",
                int(BUY_WARMUP.total_seconds()),
            )
            SystemEventRepository.log(
                "BUY",
                f"{stock_code} {stock_name} {quantity}주 @ {current_price:,}원 [{sub_strategy}]",
                "INFO",
            )
            weight_str = f" (비중 {opt_info['final_weight']:.2f}x)" if opt_info else ""
            if sc is not None:
                # 1A/Pullback: 점수 기반 진입 — 실제 점수/커트라인/항목별 breakdown 표시
                basis_str = (
                    f"판단근거: 점수 {sc:.2f}/{info.get('score_threshold', 0):.2f} "
                    f"({info.get('score_breakdown', '')}) | 체결강도 {strength_val:.0f}%"
                )
            else:
                # 1B/1L: 점수가 아니라 FSM 상태·체결강도 지속 기반 진입
                basis_str = f"판단근거: 체결강도 {strength_val:.0f}%"
            _notify(
                f"매수 체결 [{sub_strategy}]\n종목: {stock_name} ({stock_code})\n"
                f"수량: {quantity}주 @ {current_price:,}원{weight_str}\n"
                f"금액: {current_price * quantity:,}원\n"
                f"조건검색식: {cond_name}\n"
                f"매수이유: {display_reason}\n"
                f"{basis_str}",
                target="order",
            )
            self._mark_watch_bought(stock_code)
        finally:
            self.pending.discard(stock_code)
            self._pending_strategy.pop(stock_code, None)

    # ========================================
    # 워치리스트
    # ========================================
    def _record_watch_list(self, stock_code, stock_name, phase, info, cond_name=""):
        # 후보 점수는 **컷라인 대비 비율**로 저장한다 (2026-08-01 변경).
        # 슬롯교체(core/slot_replacement.py)는 "후보점수 >= 정체종목점수 x1.2"로
        # 비교하는데, 전략마다 점수 스케일이 완전히 달라서(1A는 체결강도 0~300,
        # Pullback은 0~9점) 원점수를 그대로 비교하면 항상 1A가 이긴다.
        # 비율(1.0 = 자기 컷라인 정확히 충족)로 통일하면 전략이 달라도
        # "자기 기준을 얼마나 넘겼나"라는 같은 축에서 비교된다.
        if info.get("score") is not None:
            self._watch_scores[stock_code] = self._score_ratio(info)
        # 재평가 후보 등록은 DB 기록과 분리 — DB에 이미 썼는지는
        # _watch_db_written으로 판단한다(2026-08-01). 예전엔 둘 다
        # watch_list_today로 판단해서, 지연평가 경로가 먼저 넣어둔 종목은
        # DB 행이 영영 안 남았다(백테스트/틱아카이브 유니버스에서 누락).
        self.watch_list_today.add(stock_code)
        if stock_code in self._watch_db_written:
            return
        try:
            extra = {}
            for k in ("ma5", "current_price", "volume_ratio"):
                if k in info:
                    extra[k] = info[k]
            if "surge_rate" in info:
                extra["surge_rate"] = info["surge_rate"] * 100
            if "reason" in info:
                extra["reason_not_bought"] = info["reason"]
            # 어떤 조건검색식이 이 종목을 편입시켰는지 매수 여부와 무관하게
            # 보존 (2026-07-31) — 기존엔 trades.entry_reason에만 "[조건명] ..."
            # 프리픽스로 남아서 매수 안 된 후보는 출처가 영구 소실됐고, 그 결과
            # 일일 백테스트가 조건검색식별 진입 로직 차이(눌림목자동 skip_setup_check
            # 등)를 전혀 재현할 수 없었다. 최초 평가 시점 값의 스냅샷이며, 이후
            # self._cond_names가 승격 병합돼도 이미 기록된 이 행은 갱신되지 않는다.
            if cond_name:
                extra["cond_name"] = cond_name
            WatchListRepository.add(
                stock_code=stock_code, stock_name=stock_name, phase=phase, **extra
            )
            self._watch_db_written.add(stock_code)
        except Exception as e:
            logger.warning("[%s] 워치리스트 DB 기록 실패(후보 등록은 유지): %s", stock_code, e)

    def _mark_watch_bought(self, stock_code: str):
        try:
            for w in WatchListRepository.find_by_date(self._now().date()):
                if w["stock_code"] == stock_code and not w.get("is_bought"):
                    WatchListRepository.mark_bought(w["id"])
                    break
        except Exception as e:
            logger.warning("[%s] mark_bought 실패: %s", stock_code, e)

    # ========================================
    # 청산 (5/22 개편: 손절 -2.5% / 익절캡 +2.5% / 20MA 이탈 / 시간정리)
    # ========================================
    def on_price_update(self, stock_code: str, current_price: float):
        pos = self.holdings.get(stock_code)
        if not pos or not current_price:
            return
        if stock_code in self.sell_blocked:
            return

        buy_price = pos["buy_price"]
        if not buy_price:
            return

        if current_price > pos["highest_price"]:
            pos["highest_price"] = current_price

        # 저점 추적 — 손실 반등 하이브리드 매도가 "저점 대비 얼마나 올라왔나"를
        # 판단하는 기준선 (2026-07-31). 매수가로 초기화해서, 매수 후 한 번도
        # 밀리지 않은 종목은 저점=매수가가 되어 반등 판정이 성립하지 않는다.
        low = pos.get("lowest_price")
        if low is None or current_price < low:
            pos["lowest_price"] = current_price

        warmup_until = pos.get("warmup_until")
        if warmup_until and self._now() < warmup_until:
            return

        gross_rate = (current_price - buy_price) / buy_price if buy_price > 0 else 0.0
        net_rate = gross_rate - ROUND_TRIP_COST
        exit_reason = None

        if gross_rate <= STOP_LOSS_RATE:
            exit_reason = f"손절 가격{gross_rate*100:.2f}% (순 {net_rate*100:.2f}%)"

        # 2) 익절 — 평상시엔 트레일링은 1L(주도주)에만, 나머지는 항상 flat 익절캡.
        # 지수 급락(-5%↓) 대응 모드에서는 전략 구분 없이 트레일링 없이
        # flat SEVERE_CRASH_TAKE_PROFIT로 통일(빠른 익절 확정). (2026-07-28)
        if exit_reason is None:
            if self._is_severe_crash():
                if net_rate >= SEVERE_CRASH_TAKE_PROFIT:
                    exit_reason = (
                        f"익절(지수급락 대응) 순+{net_rate*100:.2f}% (가격 +{gross_rate*100:.2f}%)"
                    )
            elif pos.get("sub_strategy") == "1L" and not self._is_early_buy(pos):
                # 개장초반(09:01~09:10) 매수분은 1L도 트레일링 대신 flat 1.5%
                # (2026-07-30 사용자 지정) — 아래 else의 캡 분기로 넘어간다.
                highest = pos.get("highest_price", buy_price)
                peak_net = self._net_rate(buy_price, highest)
                if peak_net >= TRAIL_ACTIVATE:
                    pos["trail_armed"] = True
                if pos.get("trail_armed"):
                    trail_line = highest * (1 - TRAIL_GIVEBACK)
                    if current_price <= trail_line:
                        give = (current_price - highest) / highest * 100
                        exit_reason = (
                            f"트레일링 고점-{TRAIL_GIVEBACK*100:.0f}% "
                            f"({give:+.2f}%, 순 {net_rate*100:+.2f}%)"
                        )
            else:
                cap, cap_label = self._take_profit_cap(pos)

                # 동적 상향 갈림길 — 순 +1.0% 도달 시점에 체결강도로 판단
                # (2026-07-31). 가격 트리거이므로 틱이 들어오는 여기서 처리하고,
                # 거래량 확인이 필요한 '즉시매도'는 _update_dynamic_caps에서 담당.
                if pos.get("tp_cap") is None and net_rate >= TP_UPGRADE_TRIGGER:
                    rising = self._is_strength_rising_vs_entry(pos, stock_code)
                    # 주도테마 부합 가산점 (2026-07-31 사용자 지정) — 매수 "전"
                    # 조건이 아니라 매수 "후" 판단으로 전환. 1L이 하던 "주도테마
                    # 소속이어야 매수"라는 사전 게이트는 없앴지만(1L 자체 주석
                    # 처리), 매수 이후 시점에 마침 주도테마에 부합해 있으면
                    # 그것도 강도상승과 동등한 '가산점'으로 취급해 동적캡을
                    # 올린다 — 강도가 애매(None/False)해도 주도테마면 구제됨.
                    is_leading = bool(
                        self.theme_mgr and self.theme_mgr.is_leading_theme_stock(stock_code)
                    )
                    if rising is True or is_leading:
                        reasons = []
                        if rising is True:
                            reasons.append("강도상향")
                        if is_leading:
                            reasons.append("주도테마")
                        label = "+".join(reasons)
                        pos["tp_cap"] = TP_CAP_UPGRADED
                        pos["tp_cap_label"] = label
                        cap, cap_label = TP_CAP_UPGRADED, label
                        logger.info(
                            "[%s] %s 익절캡 상향 -> %.1f%% (순+%.2f%% 도달, 사유=%s, "
                            "체결강도 진입 %.0f -> 현재 %.0f, 테마=%s)",
                            stock_code, pos.get("stock_name", ""),
                            TP_CAP_UPGRADED * 100, net_rate * 100, label,
                            pos.get("entry_strength") or 0,
                            self._current_strength(stock_code),
                            self.theme_mgr.code_to_theme.get(stock_code, "-")
                            if self.theme_mgr else "-",
                        )
                    elif rising is False:
                        exit_reason = (
                            f"익절 조기확정(강도 미상승) 순+{net_rate*100:.2f}% "
                            f"(가격 +{gross_rate*100:.2f}%)"
                        )
                    # rising is None and not is_leading = 판단 불가 -> 기본 캡 유지

                if exit_reason is None and net_rate >= cap:
                    exit_reason = (
                        f"익절 캡({cap_label} {cap*100:.1f}%) 순+{net_rate*100:.2f}% "
                        f"(가격 +{gross_rate*100:.2f}%)"
                    )

        # 3) 정체 정리 — 30분을 다 기다리지 않고 15분에 한 번 끊는다 (2026-08-01).
        # 실측(07-28~31, 73건): '시간정리 30분' 7건이 슬롯·분의 25%를 먹고 평균
        # -0.78%였고, ±0.5% 이내로 끝난 무의미 청산 14건이 185분(22%)을 점유했다.
        # 반면 돈을 번 청산(익절+트레일링)은 슬롯·분의 19%만 썼다 — 슬롯의
        # 기회비용을 생각하면 '아무 방향도 못 잡은 자리'를 오래 들고 있을 이유가
        # 없다. 30분 컷은 최후 방어선으로 그대로 유지한다.
        if exit_reason is None:
            held = self._now() - pos["buy_time"]
            if held >= HOLDING_TIMEOUT:
                exit_reason = f"시간정리 30분 (순 {net_rate*100:+.2f}%)"
            elif (
                held >= timedelta(minutes=DEAD_POSITION_MIN)
                and abs(net_rate) <= DEAD_POSITION_BAND
            ):
                exit_reason = (
                    f"정체 정리 {DEAD_POSITION_MIN}분 (순 {net_rate*100:+.2f}%, "
                    f"±{DEAD_POSITION_BAND*100:.1f}% 밴드 — 슬롯 기회비용)"
                )

        if exit_reason:
            self._execute_sell(stock_code, current_price, exit_reason)

    def _is_strength_rising_vs_entry(self, pos: dict, stock_code: str):
        """진입 대비 체결강도가 유의하게 상승했는지 3값 판정 (2026-07-31).
        True=상승(캡 상향) / False=미상승(조기 익절확정) / None=판단불가.
        None을 따로 두는 이유: 강도 데이터가 없을 때 '미상승'으로 몰면 멀쩡한
        포지션을 +1.0%에서 전부 잘라버리게 되므로, 그때는 기본 캡을 유지한다."""
        entry_s = pos.get("entry_strength") or 0.0
        if entry_s <= 0:
            return None
        if not (self.phase1b and getattr(self.phase1b, "trade_flow", None)):
            return None
        try:
            cur_s = self._current_strength(stock_code)
        except Exception:
            return None
        # 중립값(100.0)은 '틱 부족으로 판단 불가'를 뜻하므로 상승으로 오해하지 않음
        # (trade_flow.compute_strength가 최소틱수 미달 시 STRENGTH_NEUTRAL 반환)
        if cur_s <= 0 or cur_s == STRENGTH_NEUTRAL:
            return None
        return cur_s >= entry_s * TP_UPGRADE_STRENGTH_RATIO

    def _update_dynamic_caps(self):
        """익절캡이 상한(2.5%)인 종목의 조기 이탈 판정 (2026-07-30, tick() 주기 실행).

        체결강도 하락 AND 거래량 하락이 동시에 오면 캡을 기다리지 않고 즉시 매도.
        (사용자 스펙이 AND — OR로 하면 과민해서 백테스트상 오히려 손해였음:
         건당 -0.328% -> -0.532%)

        캡 '상향'은 여기가 아니라 on_price_update에서 처리한다(2026-07-31) —
        순 +1.0% 도달이라는 가격 트리거가 필요해서 틱 경로에 있어야 하고,
        여기에 두면 가격과 무관하게 강도만으로 올라가 백테스트 설계와 달라진다.

        비용 설계: 강도는 메모리(trade_flow)라 매번 계산해도 무료이지만 거래량은
        REST가 필요하다. 그래서 '강도 하락'이 먼저 확인된 종목만,
        종목당 TP_VOL_CHECK_SEC 간격으로 거래량을 조회한다."""
        if not self.holdings:
            return
        now_dt = self._now()
        for code in list(self.holdings.keys()):
            pos = self.holdings.get(code)
            if not pos or code in self.pending or code in self.sell_blocked:
                continue
            warmup_until = pos.get("warmup_until")
            if warmup_until and now_dt < warmup_until:
                continue  # 매수 직후 워밍업 중엔 판단 보류(기존 관례와 동일)

            entry_s = pos.get("entry_strength") or 0.0
            if entry_s <= 0:
                continue  # 진입강도 기록이 없으면 비교 불가
            try:
                cur_s = self._current_strength(code)
            except Exception:
                continue

            # 중립값(틱 부족으로 판단 불가)을 "하락"으로 오판하지 않는다 (2026-07-31
            # 실거래로 발견 — 매수 66/69초 만에 강도 166->100/253->100으로
            # "동적캡 즉시매도"가 발동했는데, 가격은 거의 안 움직였고(0.00%/-0.17%)
            # 실제로는 최근 10초 틱이 부족해 compute_strength가 중립값(100)을
            # 반환한 것뿐이었다. on_price_update의 _is_strength_rising_vs_entry는
            # 이미 같은 이유로 중립값을 별도 처리하는데(07-31 도입), 여기 하락
            # 판정에는 그 방어가 빠져있었다 — 진입강도가 100 초과인 대부분의
            # 포지션에서 데이터 부족을 곧바로 "하락"으로 오인하게 되는 구조였다.
            if cur_s <= 0 or cur_s == STRENGTH_NEUTRAL:
                continue  # 판단 불가 -> 보수적으로 유지(하락 확정 아님)

            # 체결강도 하락은 두 경로(익절캡 조기이탈 / 손실반등 매도) 공통 전제.
            # 유지 중이면 어느 쪽도 해당 없으므로 거래량 조회(REST) 전에 끊는다.
            if cur_s >= entry_s * TP_DECLINE_STRENGTH_RATIO:
                continue

            price = self.phase1b.trade_flow.get_latest_price(code) if self.phase1b else None
            if not price:
                try:
                    c1 = self.api.get_minute_candles(code, interval=1, count=1)
                    price = float(c1[0]["close"]) if c1 else None
                except Exception:
                    price = None
            if not price:
                continue

            cap, _ = self._take_profit_cap(pos)
            # 경로 A(기존): 상한캡(2.5%) 종목의 조기 이탈 — 1A처럼 처음부터
            # 2.5%인 종목과 on_price_update에서 강도상향된 종목 둘 다 포함.
            # 단, 1L은 제외한다(2026-07-31 실거래로 발견) — 1L은 익절 메커니즘이
            # 트레일링 전용인데, _take_profit_cap이 sub="1L"을 특별 케이스하지
            # 않아 fallback 기본캡(2.5%)을 반환하고 이게 TP_CAP_UPGRADED(2.5%)와
            # 우연히 같아서 cap_exit이 잘못 True가 됐다. 그 결과 1L 포지션이
            # 트레일링과 무관하게 이 캡 조기이탈 체크에 걸려 매수 66/69초 만에
            # "동적캡 즉시매도"로 조기청산되는 실거래 사고가 있었다(010120, 067310).
            cap_exit = pos.get("sub_strategy") != "1L" and cap >= TP_CAP_UPGRADED
            # 경로 B(신규): 손실 종목이 저점에서 반등했으나 그 반등이 강도로
            # 뒷받침되지 않는 경우 — 손실 최소화 청산.
            loss_rebound = self._is_loss_rebound_exit(pos, price, now_dt)
            if not cap_exit and not loss_rebound:
                continue

            last = self._tp_vol_checked_at.get(code)
            if last is not None and (now_dt - last).total_seconds() < TP_VOL_CHECK_SEC:
                continue
            self._tp_vol_checked_at[code] = now_dt
            try:
                candles = self._get_merged_candles(code, interval=1, count=30)
                vol_ratio = self._volume_ratio(candles) if candles else None
            except Exception as e:
                logger.warning("[%s] 동적캡 거래량 조회 실패: %s", code, e)
                continue
            if vol_ratio is None or vol_ratio >= TP_DECLINE_VOLUME_RATIO:
                continue  # 거래량은 아직 살아있음 -> 계속 보유

            net_rate = self._net_rate(pos["buy_price"], price) * 100
            if loss_rebound and not cap_exit:
                low = pos.get("lowest_price") or pos["buy_price"]
                bounce = (price - low) / low * 100 if low else 0.0
                reason = (
                    f"손실반등 하이브리드 매도 (저점 대비 +{bounce:.2f}% 반등 후 "
                    f"체결강도 {entry_s:.0f}->{cur_s:.0f}, 거래량 x{vol_ratio:.2f}, "
                    f"순 {net_rate:+.2f}% — 손절 대기 대신 손실 축소)"
                )
            else:
                reason = (
                    f"동적캡 즉시매도 (체결강도 {entry_s:.0f}->{cur_s:.0f}, "
                    f"거래량 x{vol_ratio:.2f}, 순 {net_rate:+.2f}%)"
                )
            self._execute_sell(code, price, reason)

    def _is_loss_rebound_exit(self, pos: dict, price: float, now_dt) -> bool:
        """손실 반등 하이브리드 매도 대상인지 (2026-07-31 사용자 지정).

        조건: ① 현재 순손익이 손실 ② 매수 후 최소 보유시간 경과
              ③ 당일 저점 대비 LOSS_REBOUND_MIN(+1%) 이상 반등한 상태.
        여기서 True가 나와도 호출부에서 체결강도 하락 AND 거래량 하락을 함께
        확인해야 실제 매도된다 — 즉 "반등했지만 그 반등에 힘이 없다"는
        3중 확인 구조. 손절(-3%)은 그대로 남아 최후 방어선 역할을 한다."""
        buy_price = pos.get("buy_price")
        if not buy_price or not price:
            return False
        if self._net_rate(buy_price, price) >= 0:
            return False  # 손실 구간에서만 작동(이익 구간은 익절캡 담당)

        buy_time = pos.get("buy_time")
        if buy_time is None:
            return False
        if (now_dt - buy_time) < timedelta(minutes=LOSS_REBOUND_MIN_HOLD_MIN):
            return False  # 매수 직후 노이즈로 잘리는 것 방지

        low = pos.get("lowest_price")
        if not low or low <= 0:
            return False  # 저점 기록 없음(틱 미수신) -> 판단 보류
        return (price - low) / low >= LOSS_REBOUND_MIN

    @staticmethod
    def _is_early_buy(pos: dict) -> bool:
        """개장초반(09:00~09:10) 매수분인지. 1L 트레일링 예외 판정에도 쓴다(1L은
        현재 주석처리 상태라 사실상 1A/Pullback/1B의 익절캡 판정에만 쓰임)."""
        buy_time = pos.get("buy_time")
        if buy_time is None:
            return False
        try:
            return GROUP_A_START <= buy_time.time() < EARLY_WINDOW_END
        except AttributeError:
            return False

    @classmethod
    def _take_profit_cap(cls, pos: dict) -> tuple[float, str]:
        """포지션별 익절 캡과 표시용 라벨 (2026-07-30).
        기본 캡은 매수 시점 기준으로 고정하되, 동적캡 로직이 올려둔
        pos["tp_cap"]이 있으면 그 값이 우선한다.
        1L은 트레일링을 쓰므로 평상시엔 이 함수를 타지 않지만(호출부 분기),
        개장초반 매수분은 트레일링 대신 여기의 1.5%를 쓴다."""
        override = pos.get("tp_cap")
        if override is not None:
            return float(override), pos.get("tp_cap_label", "동적")

        if cls._is_early_buy(pos):
            return TAKE_PROFIT_CAP_EARLY, "개장초반"

        sub = pos.get("sub_strategy")
        if sub == "1B":
            return TAKE_PROFIT_CAP_1B, "1B"
        if sub == "1A_눌림":
            return TAKE_PROFIT_CAP_PULLBACK, "눌림"
        return TAKE_PROFIT_CAP, "기본"

    def _execute_sell(self, stock_code, current_price, exit_reason):
        pos = self.holdings.get(stock_code)
        if not pos or stock_code in self.pending:
            return
        if stock_code in self.sell_blocked:
            return

        logger.info(
            "청산 트리거 [%s] %s | 사유: %s | 현재가 %s",
            stock_code,
            pos.get("stock_name", ""),
            exit_reason,
            f"{current_price:,}",
        )

        self.pending.add(stock_code)
        try:
            quantity = pos.get("qty", pos.get("buy_quantity"))
            result = self.order_manager.sell(stock_code, quantity, price=0)

            if not result or not result.get("success"):
                err = (result or {}).get("error", "unknown")
                cnt = self.sell_fail_count.get(stock_code, 0) + 1
                self.sell_fail_count[stock_code] = cnt

                logger.error(
                    "[%s] 매도 실패 (%d/%d): %s", stock_code, cnt, MAX_SELL_FAIL, err
                )
                SystemEventRepository.log(
                    "ORDER_FAIL",
                    f"SELL {stock_code}: {err} ({cnt}/{MAX_SELL_FAIL})",
                    "ERROR",
                )

                if cnt >= MAX_SELL_FAIL:
                    self.sell_blocked.add(stock_code)
                    stock_name = pos.get("stock_name", stock_code)
                    self.holdings.pop(stock_code, None)
                    logger.warning(
                        "[%s] %s 매도 %d회 실패 -> 차단", stock_code, stock_name, cnt
                    )
                    SystemEventRepository.log(
                        "SELL_BLOCKED",
                        f"{stock_code} 매도 {cnt}회 실패 -> 차단",
                        "WARNING",
                    )
                    _notify(
                        f"매도 차단\n{stock_name} ({stock_code})\n"
                        f"매도 {cnt}회 실패 -> 수동 확인 필요"
                    )
                else:
                    _notify(
                        f"매도 실패 ({cnt}/{MAX_SELL_FAIL})\n"
                        f"{pos.get('stock_name', stock_code)} ({stock_code})\n사유: {err}"
                    )
                return

            self.sell_fail_count.pop(stock_code, None)
            self.sold_at[stock_code] = self._now()

            # 매도 주문은 이미 체결됨 — DB 기록이 실패(또는 trade_id 없음)해도
            # 아래 포지션 정리(self.holdings 제거)는 반드시 이어져야 한다.
            if pos.get("trade_id") is not None:
                try:
                    TradeRepository.update_sell(
                        trade_id=pos["trade_id"],
                        sell_price=current_price,
                        sell_quantity=quantity,
                        exit_reason=exit_reason,
                    )
                except Exception as e:
                    logger.exception(
                        "[%s] 매도 DB 기록 실패 (청산은 정상 처리): %s", stock_code, e
                    )
                    SystemEventRepository.log(
                        "DB_LOG_FAIL", f"update_sell {stock_code}: {e}", "ERROR"
                    )
            else:
                logger.warning(
                    "[%s] trade_id 없음(매수 DB 기록 실패 이력) — 매도 DB 갱신 스킵",
                    stock_code,
                )

            # 순손익 (수수료/세금 차감)
            net_profit = self._net_profit(pos["buy_price"], current_price, quantity)
            self._daily_realized += net_profit  # MDD 누적
            gross_rate = self._gross_rate(pos["buy_price"], current_price) * 100
            net_rate = self._net_rate(pos["buy_price"], current_price) * 100
            stock_name = pos["stock_name"]
            sub = pos.get("sub_strategy", "?")
            del self.holdings[stock_code]

            # 장중 전략 성과에 반영 (2026-07-31) — 이후 이 전략의 점수 컷라인이
            # 자동 조정된다. net_rate는 위에서 %로 환산돼 있으므로 소수로 되돌림.
            try:
                self.perf.record(sub, net_rate / 100.0)
                self.perf.log_tier_change(sub)
                # [제안 B] 조건검색식 축에도 같은 성과를 기록 (2026-08-01).
                # 전략 축과 독립된 키라 서로 간섭하지 않는다.
                cond_key = pos.get("cond_key")
                if cond_key:
                    self.perf.record(cond_key, net_rate / 100.0)
                    self.perf.log_tier_change(cond_key)
            except Exception as e:
                logger.warning("[%s] 전략성과 기록 실패: %s", stock_code, e)

            # 실제 손실로 마감된 청산은 사유(손절/슬롯교체/시간정리) 무관하게 당일
            # 재매수 금지. 익절만 기존 3분 쿨다운 후 재진입 허용. (2026-07-29 수정 —
            # 기존엔 "손절"로 시작하는 사유만 차단해서, 슬롯교체로 손실 나간 종목이
            # 3분 쿨다운만 지나면 바로 재매수돼 같은 종목에서 반복 손실이 났음.
            # 예: 스피어(347700) 09:38→09:50 슬롯교체(-59,899)→09:53 재매수→10:04
            # 또 슬롯교체(-9,172)→10:07 재매수→10:17 결국 손절(-130,219), 합계
            # -199,290원. 이제 09:50 시점에 바로 차단되어 반복이 끊긴다.)
            if net_profit < 0:
                self._stoploss_blocked.add(stock_code)
                logger.info(
                    "[%s] 손실 청산(%s, 순%.2f%%) → 당일 재매수 차단",
                    stock_code, exit_reason, net_rate,
                )

            logger.info(
                "SELL [%s] %s %d주 @ %s원 -> %s | 순손익 %s원 (순 %.2f%%, 가격 %.2f%%) [%s]",
                stock_code,
                stock_name,
                quantity,
                f"{current_price:,}",
                exit_reason,
                f"{net_profit:+,.0f}",
                net_rate,
                gross_rate,
                sub,
            )
            SystemEventRepository.log(
                "SELL",
                f"{stock_code} {stock_name} {quantity}주 @ {current_price:,}원 [{sub}] | "
                f"{exit_reason} | 순손익 {net_profit:+,.0f}원",
                "INFO",
            )
            emoji = "+" if net_profit > 0 else "-"
            _notify(
                f"매도 체결 [{sub}] ({emoji})\n종목: {stock_name} ({stock_code})\n"
                f"수량: {quantity}주 @ {current_price:,}원\n"
                f"순손익: {net_profit:+,.0f}원 (순 {net_rate:+.2f}%, 가격 {gross_rate:+.2f}%)\n"
                f"사유: {exit_reason}\n재매수 차단: {int(REBUY_COOLDOWN.total_seconds()/60)}분"
            )
        finally:
            self.pending.discard(stock_code)

    # ========================================
    # 타임아웃
    # ========================================
    def check_timeouts(self):
        now = self._now()
        for code in list(self.holdings.keys()):
            pos = self.holdings[code]
            warmup_until = pos.get("warmup_until")
            if warmup_until and now < warmup_until:
                continue
            if now - pos["buy_time"] < HOLDING_TIMEOUT:
                continue
            if code in self.sell_blocked:
                continue
            try:
                # 병합 메서드 사용
                candles = self._get_merged_candles(code, interval=1, count=1)
                if candles:
                    self._execute_sell(code, candles[0]["close"], "시간정리 30분")
            except Exception as e:
                logger.exception("[%s] 타임아웃 청산 실패: %s", code, e)
