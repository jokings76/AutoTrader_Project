"""
매매 전략 매니저 — 1A/Pullback/1B/1L 통합 + 하이브리드 청산 + 동적 비중 + 수수료 반영
(2026-07-27 재설계: 1S/1N/Phase2/Phase3 삭제, 슬롯 구조 개편)

시간대:
  1A       (09:01 ~ 10:30 기본, 10:30 ~ 14:50 점수 상향): 거래량증가지속 + 체결강도지속 + 점수
  Pullback (09:01 ~ 10:30): 눌림목 반등 점수 + VWAP AND 필터
  1B       (Pullback 미체결 후보 감시, 5단계 FSM): 매도벽 등장→축소+강도상승→소실
  1L       (09:01 ~ 10:50): 주도테마 + 체결강도 100 이상 2분 지속 유지 시 즉시매수

슬롯: 1A/Pullback/1L/1B 각각 자체 상한 3개 + 전체 합산 상한 6개(MAX_HOLDINGS) 공유.
      1L/1B는 실시간 틱 콜백에서 즉시 매수(우선권), 1A/Pullback은 조건검색 이벤트 경로.

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

# 1A/Pullback 공통 시간 윈도우
GROUP_A_START = time(9, 1)
PULLBACK_END = time(10, 30)
PHASE1A_TIGHTEN_TIME = time(10, 30)  # 이 시각부터 1A 점수 상향
PHASE1A_END = time(14, 50)
# 주도주상위 조건검색은 GROUP_A_START(09:01)부터 그대로 감시하되, 나머지
# 조건검색식(체결강도100/돌파자동매매용/관심종목감시)은 09:20부터 감시 시작.
# (2026-07-29) on_condition_hit에서 cond_name 기준으로 적용.
OTHER_COND_START = time(9, 20)
# 1L(주도주) 시간 윈도우
LEADING_START = time(9, 1)
LEADING_END = time(10, 50)

# 진입 조건 (Phase 1A 설정값 사용)
SURGE_THRESHOLD = PHASE_1A["surge_threshold"]
MA_TOUCH_TOLERANCE = PHASE_1A["ma_tolerance"]
VOLUME_SURGE_RATIO = PHASE_1A["volume_surge_ratio"]
VOLUME_LOOKBACK = 5

# 1A 점수 커트라인 (10:30 이후 상향 + 지수경보(CAUTION) 시 추가 상향)
PHASE1A_SCORE_NORMAL = 6.5
PHASE1A_SCORE_TIGHT = 8.5
PHASE1A_SCORE_CAUTION_BONUS = 1.0

# Pullback 반등 확인용 OBV(누적거래량) 확인 구간 — 몇 봉 전과 비교할지.
# (2026-07-29 신규: 거래량 없는 가짜 반등을 VWAP과 함께 걸러내는 용도)
OBV_LOOKBACK = 5

# 지수 방어 로직 임시 비활성화 스위치 (2026-07-28: 지수 급락 중 테스트 진행을 위해 OFF.
# 프로그램이 매끄럽게 동작 확인되면 True로 되돌릴 것)
MARKET_DEFENSE_ENABLED = False

# 매수 진입 등락률 상한 (당일 시가 대비). 이미 많이 오른 종목을 추격매수하는 걸
# 막기 위한 필터 — 1A/Pullback/1B/1L 전체 전략에 동일 적용. (2026-07-28)
MAX_ENTRY_CHANGE_PCT = 12.0

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
PHASE1A_MAX_SLOTS = PHASE_1A["max_slots"]
PULLBACK_MAX_SLOTS = 3
PHASE1B_MAX_SLOTS = PHASE_1B["max_slots"]

MAX_WATCH_SLOTS = 10
WATCH_TIMEOUT = timedelta(minutes=10)

# 주도주 우선 진입 (주도테마 + 체결강도 100 이상 2분 지속)
LEADING_MAX_SLOTS = 3
LEADING_STRENGTH_MIN = 100.0
LEADING_SUSTAIN = timedelta(minutes=2)

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
        self.watch_list_today: set[str] = set()
        self.pending: set[str] = set()
        self._stock_names: dict[str, str] = {}
        self._cond_names: dict[str, str] = {}  # stock_code -> 최초 편입 조건검색식 이름
        self._opening_prices: dict[str, float] = {}  # stock_code -> 당일 시가 (등락률 상한 체크용)
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
            "window_ok": 0,    # 시간창(09:01~10:50) 안이었던 틱
            "both_ok": 0,      # 3조건 모두 충족(=타이머 유지)이었던 틱
        }
        self._l1_diag_last_report = self._now()
        self._l1_max_sustain_sec = 0.0          # 오늘 도달한 최장 연속 유지 시간
        self._l1_reset_logged_at: dict[str, datetime] = {}  # 리셋 로그 throttle
        self._l1_block_logged_at: dict[str, datetime] = {}  # 차단 로그 throttle

        # 1B 반등확증 대기 {code: {ref_high, since, last_check}} (2026-07-30)
        self._1b_confirm: dict[str, dict] = {}

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

        logger.info(
            "DB 복원: 보유 %d (1A=%d, 눌림=%d, 1B=%d, 1L=%d) / 워치 %d / 워밍업 %ds",
            len(self.holdings),
            self.count_holdings_by_strategy("1A"),
            self.count_holdings_by_strategy("1A_눌림"),
            self.count_holdings_by_strategy("1B"),
            self.count_holdings_by_strategy("1L"),
            len(self.watch_list_today),
            int(RESTART_WARMUP.total_seconds()),
        )

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

        if self.phase1b and now.time() >= PULLBACK_END:
            for code in list(self.phase1b.watched):
                if code not in self.holdings:
                    self.phase1b.stop_watching(code)

        self._check_1b_confirmations()
        self._update_dynamic_caps()
        self.check_timeouts()

    # ========================================
    # Phase 판별 (09:01~14:50 그룹A 활성 여부)
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

    def can_buy_more(self) -> bool:
        if not self.risk_can_trade():
            return False
        if self._get_market_defense_mode() == "HALT":
            return False
        if self._now() < self.quarantine_until:
            return False
        return len(self.holdings) < MAX_HOLDINGS

    def count_holdings_by_strategy(self, sub: str) -> int:
        return sum(1 for h in self.holdings.values() if h.get("sub_strategy") == sub)

    def can_buy_phase1a(self) -> bool:
        # 1A: 09:01~14:50 (10:30부터 점수 커트라인 상향은 evaluate_new_intensity_strategy에서)
        return (
            self.can_buy_more()
            and self.count_holdings_by_strategy("1A") < PHASE1A_MAX_SLOTS
            and GROUP_A_START <= self._now().time() < PHASE1A_END
        )

    def can_buy_pullback(self) -> bool:
        # 눌림목: 09:01~10:30, 1A와 시간대는 겹치지만 슬롯은 별도(sub_strategy="1A_눌림")
        return (
            self.can_buy_more()
            and self.count_holdings_by_strategy("1A_눌림") < PULLBACK_MAX_SLOTS
            and GROUP_A_START <= self._now().time() < PULLBACK_END
        )

    def can_buy_phase1b(self) -> bool:
        return (
            self.can_buy_more()
            and self.count_holdings_by_strategy("1B") < PHASE1B_MAX_SLOTS
            and GROUP_A_START <= self._now().time() < PULLBACK_END
        )

    def can_buy_leading(self) -> bool:
        # 주도주 우선 진입: 09:01~10:50
        return (
            self.can_buy_more()
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

        phase = self.get_current_phase()
        if phase is None:
            return
        if stock_code in self.holdings or stock_code in self.pending:
            return

        blocked, reason = self._is_rebuy_blocked(stock_code)
        if blocked:
            logger.info("[%s] %s 매수 차단: %s", stock_code, stock_name, reason)
            return

        self._stock_names[stock_code] = stock_name
        # 이미 실제 조건명이 기록돼 있으면 "기타"/"알수없음" 같은 부실한 값으로
        # 덮어쓰지 않음 (2026-07-30). 실시간 WS 편입 이벤트(_on_signal)가 cond_seq를
        # 못 읽어 "기타"로 넘어오는 경우가 있는데, 이게 초기 스냅샷이 이미 정확히
        # 기록해둔 "주도주상위+..." 같은 값을 덮어써버리면 OTHER_COND_START(09:20)
        # 게이트가 주도주상위 종목까지 잘못 지연시킴 — 오늘 실전에서 이 때문에
        # SK이터닉스 등 9종목이 09:01~09:20 사이 18분간 평가 자체가 멈췄던 것 확인.
        existing_cond = self._cond_names.get(stock_code)
        if not existing_cond or existing_cond in ("기타", "알수없음"):
            self._cond_names[stock_code] = cond_name

        # 거래대금 폭발 이력(explosion_scorer) 준비는 여기서 더 이상 안 함 —
        # 종가베팅 스캐너(main.py task_closing_bet_scanner, 14:50)에서만 쓰이는
        # 데이터인데 조건검색 걸릴 때마다(하루 수십 회) 미리 계산해두느라
        # REST 호출이 과다해지던 문제(429 다발)가 있어서, 실제 필요한 시점인
        # 14:50 스캔 때 그 시점 후보 종목들만 대상으로 계산하도록 이관함.
        # (2026-07-28: 조건검색에 N봉 신고가+거래량 필터를 사용자가 직접 추가해서
        # 1차 필터링이 이미 상류에서 되고 있는 것도 이관 결정의 근거)

        try:
            now_t = self._now().time()
            if not (GROUP_A_START <= now_t < PHASE1A_END):
                return

            candles = self._get_merged_candles(stock_code, interval=1, count=15)
            if not candles or len(candles) < VOLUME_LOOKBACK + 1:
                logger.warning(
                    "[%s] 분봉 부족 (%d개)", stock_code, len(candles) if candles else 0
                )
                return

            current_price = int(candles[0].get("close", 0))
            open_price = int(candles[-1].get("open", 0))
            if open_price > 0:
                self._opening_prices.setdefault(stock_code, open_price)

            self._evaluate_1a_pullback_entry(
                stock_code, stock_name, phase, candles, current_price, open_price, now_t
            )

        except Exception as e:
            logger.exception("[%s] on_condition_hit 실패: %s", stock_code, e)
            SystemEventRepository.log("STRATEGY_ERROR", f"{stock_code}: {e}", "ERROR")
            _notify(f"전략 에러\n{stock_code}: {e}")

    def _evaluate_1a_pullback_entry(
        self, stock_code, stock_name, phase, candles, current_price, open_price, now_t
    ) -> bool:
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
            return False

        # 주도주상위는 기존대로 GROUP_A_START(09:01)부터 바로 평가, 나머지
        # 조건검색식(체결강도100/돌파자동매매용/관심종목감시)은 09:20 이전이면
        # 아직 평가하지 않음(2026-07-29). on_condition_hit/watchlist_reentry
        # 공유 진입점이라 여기 한 곳에 둬야 두 경로 다 일관되게 걸림 — 특히
        # task_watchlist_reentry가 15초마다 재시도하므로 on_condition_hit
        # 쪽에서만 막으면 곧바로 재평가돼서 지연이 무의미해짐.
        # watch_list_today에는 넣어둬서 09:20이 지나면 watchlist_reentry가
        # 자연히 다시 평가하게 함(단순 차단이 아니라 지연 평가).
        cond_name = self._cond_names.get(stock_code, "")
        if "주도주상위" not in cond_name and now_t < OTHER_COND_START:
            self.watch_list_today.add(stock_code)
            return False

        # ==========================================
        # 1A: 거래량증가지속 + 체결강도지속 + 점수 (09:01~14:50)
        # ==========================================
        if current_price > 0:
            ok, info = self.evaluate_new_intensity_strategy(
                stock_code, candles, current_price, open_price, now_t
            )
            self._record_watch_list(stock_code, stock_name, phase, info)
            if ok and self.can_buy_phase1a():
                self._execute_buy(stock_code, stock_name, phase, info, sub_strategy="1A")
                return True

        # ==========================================
        # Pullback: 눌림목 반등 점수 + VWAP AND (09:01~10:30, 1A와 시간대 겹침/슬롯 별도)
        # ==========================================
        if now_t < PULLBACK_END:
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

            ok, info = self.evaluate_pullback(candles, stock_code, obv_mom)
            self._record_watch_list(stock_code, stock_name, phase, info)

            # 체결강도 FSM(1B) 감시 시작 — Pullback 시간대에 들어온 후보는 눌림
            # 성립 여부와 무관하게 전부 감시 대상으로 넣는다.
            # 위치는 점수판정 앞(2026-07-29 변경 유지) — 이후 재평가 사이클에서
            # 강도 점수가 실제값을 갖도록.
            # 단, "눌림 성립(setup_ok)"을 감시 시작 조건으로 걸었던 것은 되돌림
            # (2026-07-30): 1B는 눌림목 패턴이 아니라 매도벽 소멸을 보는 독립
            # 전략이라 눌림 성립을 전제할 이유가 없고, 실제로 이 조건 때문에
            # 07-30 주도주 9종목이 전부 "눌림 미성립(되돌림 4~9%)"으로 감시조차
            # 시작되지 않아 1B 매매가 27건(07-29) -> 7건으로 급감했음.
            if (
                self.phase1b
                and self.can_buy_phase1b()
                and not self.phase1b.is_watching(stock_code)
            ):
                self.phase1b.start_watching(stock_code)

            if ok:
                if self._apply_vwap_filter(
                    stock_code, "pullback", current_price, info,
                    today_candles=today_candles,
                ):
                    if self.can_buy_pullback():
                        self._execute_buy(
                            stock_code, stock_name, phase, info, sub_strategy="1A_눌림"
                        )
                        # Pullback 자체 점수로 이미 샀으면 1B 감시는 더 필요
                        # 없음 — 안 끄면 나중에 이 종목을 팔고 난 뒤 phase1b가
                        # 여전히 감시 중이다가 별도로 재매수를 시도할 수 있음.
                        if self.phase1b and self.phase1b.is_watching(stock_code):
                            self.phase1b.stop_watching(stock_code)
                        return True
                    logger.info("[%s] pullback 조건 OK but 슬롯 부족", stock_code)
            elif info.get("reason"):
                logger.info(
                    "[%s] %s pullback 미충족: %s", stock_code, stock_name, info.get("reason")
                )

        return False

    # ========================================
    # 실시간 콜백
    # ========================================
    def on_trade(self, parsed_trade: dict, now: float = None):
        code = parsed_trade.get("stock_code")
        if not code:
            return

        if code in self.holdings:
            price = parsed_trade.get("price")
            if price:
                self.on_price_update(code, price)
            return

        # [최우선 전략] 주도테마 + 체결강도 100 이상 2분 지속 유지 → 즉시 매수 (09:01~10:50)
        if code not in self.pending:
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

        if self.phase1b and self.phase1b.is_watching(code):
            state = self.phase1b.on_trade(parsed_trade, now=now)
            if state == ChemulState.READY_TO_BUY:
                self._try_phase1b_buy(code, now)

    def _maybe_report_1l_diag(self, now_dt):
        """1L 판정 통계를 10분마다 1회 요약 로깅 (2026-07-30 진단용).
        개별 전이 로그가 하나도 안 찍히는 경우(=자격 갖춘 종목이 아예 없음)를
        구분하기 위함 — "조건이 근처까지 갔는지"를 숫자로 남긴다.
        시간창(09:01~10:50) 밖에서는 의미가 없으므로 보고하지 않는다."""
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
        code = parsed_orderbook.get("stock_code")
        if not code:
            return
        if self.phase1b and self.phase1b.is_watching(code):
            state = self.phase1b.on_orderbook(parsed_orderbook, now=now)
            if state == ChemulState.READY_TO_BUY:
                self._try_phase1b_buy(code, now)

    def _try_phase1b_buy(self, stock_code: str, now: float = None):
        """FSM이 READY_TO_BUY 도달 → 즉시 매수하지 않고 '반등확증' 대기 등록.
        (2026-07-30 변경, 근거는 PHASE1B_CONFIRM_WAIT 상수 주석 참고)"""
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
        _try_phase1b_buy(틱 콜백)가 아니라 여기서 확인한다."""
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

            if (now_dt - st["last_check"]).total_seconds() < PHASE1B_CONFIRM_CHECK_SEC:
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

    def evaluate_new_intensity_strategy(
        self,
        stock_code: str,
        candles: list,
        current_price: int,
        open_price: int,
        now_t=None,
    ) -> tuple[bool, dict]:
        """체결강도 지속 & 거래량증가지속 기반의 1A 평가 로직 (지수 방어막 포함).
        10:30부터는 점수 커트라인을 상향(PHASE1A_SCORE_TIGHT)해서 더 빡빡하게."""
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

        if final_score >= required_score:
            return True, {
                "reason": f"1A 통과 (강도:{current_intensity:.0f}, 모드:{market_mode})",
                "score": final_score,
            }
        else:
            return False, {
                "reason": f"최종 점수 부족 ({final_score:.1f}/{required_score})",
                "score": final_score,
            }

    def evaluate_pullback(self, candles, stock_code, obv_mom: float = 0.0):
        # 주의: 인자명 obv_mom은 일부러 obv_momentum(모듈 임포트된 함수명)과
        # 다르게 지음 — 같은 이름을 지역변수/인자로 쓰면 함수 스코프 내에서
        # obv_momentum이 지역변수로 취급돼 호출부(_evaluate_1a_pullback_entry)의
        # obv_momentum(...) 호출이 UnboundLocalError로 깨짐.
        return score_pullback(
            candles,
            self._volume_ratio(candles),
            self._current_strength(stock_code),
            self._adjusted_cfg(self.pullback_score_cfg),
            obv_momentum=obv_mom,
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
    def _resolve_position_amount(
        self, stock_code: str, sub_strategy: str
    ) -> tuple[int, Optional[dict]]:
        if self.optimizer is None:
            return POSITION_AMOUNT, None
        try:
            opt_info = self.optimizer.calculate_position_amount(
                stock_code, sub_strategy
            )
            amount = int(opt_info.get("amount", 0))
            if amount <= 0:
                return POSITION_AMOUNT, None
            return amount, opt_info
        except Exception:
            logger.exception(f"[{stock_code}] 비중 계산 실패, fallback")
            return POSITION_AMOUNT, None

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

    def _execute_buy(self, stock_code, stock_name, phase, info, sub_strategy):
        current_price = info["current_price"]

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

        opening_price = self._get_opening_price(stock_code)
        if opening_price:
            change_pct = (current_price - opening_price) / opening_price * 100
            if change_pct > MAX_ENTRY_CHANGE_PCT:
                logger.info(
                    "[%s] %s 매수 차단: 시가대비 +%.1f%% (상한 +%.0f%%) [%s]",
                    stock_code, stock_name, change_pct, MAX_ENTRY_CHANGE_PCT, sub_strategy,
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

        position_amount, opt_info = self._resolve_position_amount(
            stock_code, sub_strategy
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
            else:
                # 1A
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

            result = self.order_manager.buy(stock_code, quantity, price=0)
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

            # 매수 주문은 이미 체결됨 — DB 기록이 실패해도 포지션 추적(self.holdings)은
            # 반드시 이어져야 하므로 insert_buy 예외가 위로 전파되지 않게 막는다.
            try:
                trade_id = TradeRepository.insert_buy(
                    stock_code=stock_code,
                    stock_name=stock_name,
                    buy_price=current_price,
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
                "buy_price": current_price,
                "buy_quantity": quantity,
                "buy_time": self._now(),
                "stock_name": stock_name,
                "strategy_phase": phase,
                "sub_strategy": sub_strategy,
                "highest_price": current_price,
                "ma20": None,
                "ma20_updated": None,
                "trigger": info.get("trigger"),
                "opening_price": info.get("opening_price", 0.0),
                "position_weight": (opt_info or {}).get("final_weight", 1.0),
                "warmup_until": self._now() + BUY_WARMUP,
                "entry_score": sc or 0.0,
                "entry_strength": strength_val,
            }

            self.sold_at.pop(stock_code, None)
            self._buy_success_count += 1
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

    # ========================================
    # 워치리스트
    # ========================================
    def _record_watch_list(self, stock_code, stock_name, phase, info):
        if info.get("score") is not None:
            self._watch_scores[stock_code] = info["score"]
        if stock_code in self.watch_list_today:
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
            WatchListRepository.add(
                stock_code=stock_code, stock_name=stock_name, phase=phase, **extra
            )
            self.watch_list_today.add(stock_code)
        except Exception as e:
            logger.warning("[%s] 워치리스트 실패: %s", stock_code, e)

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
                    if rising is True:
                        pos["tp_cap"] = TP_CAP_UPGRADED
                        pos["tp_cap_label"] = "강도상향"
                        cap, cap_label = TP_CAP_UPGRADED, "강도상향"
                        logger.info(
                            "[%s] %s 익절캡 상향 -> %.1f%% (순+%.2f%% 도달, 체결강도 "
                            "진입 %.0f -> 현재 %.0f)",
                            stock_code, pos.get("stock_name", ""),
                            TP_CAP_UPGRADED * 100, net_rate * 100,
                            pos.get("entry_strength") or 0,
                            self._current_strength(stock_code),
                        )
                    elif rising is False:
                        exit_reason = (
                            f"익절 조기확정(강도 미상승) 순+{net_rate*100:.2f}% "
                            f"(가격 +{gross_rate*100:.2f}%)"
                        )
                    # rising is None = 강도 판단 불가 -> 기본 캡 그대로 진행

                if exit_reason is None and net_rate >= cap:
                    exit_reason = (
                        f"익절 캡({cap_label} {cap*100:.1f}%) 순+{net_rate*100:.2f}% "
                        f"(가격 +{gross_rate*100:.2f}%)"
                    )

        # 3) 시간정리 30분
        if exit_reason is None:
            if self._now() - pos["buy_time"] >= HOLDING_TIMEOUT:
                exit_reason = f"시간정리 30분 (순 {net_rate*100:+.2f}%)"

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

            cap, _ = self._take_profit_cap(pos)

            # 상한캡(2.5%) 종목만 대상 — 1A처럼 처음부터 2.5%인 종목과
            # on_price_update에서 강도상향된 종목 둘 다 포함된다.
            if cap < TP_CAP_UPGRADED:
                continue

            # 강도 하락 + 거래량 하락 -> 즉시 매도
            if cur_s >= entry_s * TP_DECLINE_STRENGTH_RATIO:
                continue  # 강도는 아직 유지 중
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
                continue  # 거래량은 아직 살아있음 -> 캡까지 계속 보유

            price = self.phase1b.trade_flow.get_latest_price(code) if self.phase1b else None
            if not price:
                try:
                    c1 = self.api.get_minute_candles(code, interval=1, count=1)
                    price = float(c1[0]["close"]) if c1 else None
                except Exception:
                    price = None
            if not price:
                logger.warning("[%s] 동적캡 즉시매도 판정됐으나 현재가 없음", code)
                continue

            net_rate = self._net_rate(pos["buy_price"], price) * 100
            self._execute_sell(
                code, price,
                f"동적캡 즉시매도 (체결강도 {entry_s:.0f}->{cur_s:.0f}, "
                f"거래량 x{vol_ratio:.2f}, 순 {net_rate:+.2f}%)",
            )

    @staticmethod
    def _is_early_buy(pos: dict) -> bool:
        """개장초반(09:01~09:10) 매수분인지. 1L 트레일링 예외 판정에도 쓴다."""
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
