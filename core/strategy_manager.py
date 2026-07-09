"""
매매 전략 매니저 — Phase 1A/1B/2/3 통합 + 하이브리드 청산 + 동적 비중 + 수수료 반영

시간대:
  Phase 1A (09:00 ~ 09:20): 시초가+5% + 5MA 터치
  Phase 1B (09:00 ~ 10:40): 체결강도 FSM (Phase 1 윈도우 신호만)
  Phase 2  (09:21 ~ 10:40): 30MA 터치  (5/22 60MA->30MA 변경: 신호 빈도up)
  Phase 3  (10:41 ~ 15:00): 체결강도 단조증가 1분 유지

청산 (5/22 개편 - 20MA 이탈 청산 도입):
  손절 -2.5%(가격)  <- 기존 -1.5%에서 완화
  익절 캡 +2.5%(순익)  <- 유지
  20MA 이탈 청산: 현재가가 20MA를 0.3% 이상 하회하면 매도  <- 트레일링 대체
  시간정리 30분
  * 익절캡은 수수료 차감한 순수익 기준 (모의 0.3% / 실전 0.015% 자동 분기)
  * 트레일링 제거 (20MA 이탈이 추세 청산 역할 대체)
  * 20MA는 보유종목별로 tick()에서 주기 갱신/캐시. 분봉 부족 시 20MA 청산만 스킵(손절/익절캡은 계속 작동)
  매도 3회 실패 차단 / 재매수 쿨다운 10분 / 매수/재시작 60초 워밍업

진입 (점수 기반 — scoring.py 위임):
  evaluate_phase1/surge/phase2 가 하드 AND 대신 가중 점수로 통과 판정.
  score_cfg.threshold_ratio=0.75 이면 기존 AND와 (거의) 동일. dry-run 로그(score/score_breakdown)
  보고 단계적으로 낮춰 near-miss 진입 허용. 임계값 낮추면 매수 기회↑=리스크 노출↑.
"""
import logging
from datetime import datetime, time, timedelta
from dataclasses import replace as _dc_replace
from typing import Optional

from core.strategy.indicators import calc_ma
from core.strategy.chemul_evaluator import ChemulState
from core.strategy.scoring import ScoreConfig, score_phase1, score_surge, score_phase2, score_pullback, score_phase3_rank
from core.strategy.entries.registry import EntryRegistry
from core.strategy.entries.surge import SurgeStrategy
from core.strategy.entries.pullback import PullbackStrategy
from core.strategy.entries.phase3 import Phase3Strategy
from core.strategy.entries.base import EntryContext
from db import TradeRepository, WatchListRepository, SystemEventRepository

try:
    from api.auth import send_telegram
except Exception as e:
    logging.getLogger(__name__).warning("send_telegram 사용 불가: %s", e)
    send_telegram = None

logger = logging.getLogger(__name__)


# -------------------------------------------------
# 수수료 (IS_MOCK 자동 분기, config에 명시하면 우선)
# -------------------------------------------------
def _load_fee_settings():
    is_mock = True
    commission = None
    tax = 0.0018
    try:
        import settings
        raw_mock = getattr(settings, "IS_MOCK", True)
        if isinstance(raw_mock, str):
            is_mock = raw_mock.strip().lower() in ("true", "1", "yes")
        else:
            is_mock = bool(raw_mock)
        # config에 명시값 있으면 우선
        c = getattr(settings, "COMMISSION_RATE", None)
        if c not in (None, "", 0):
            commission = float(c)
        t = getattr(settings, "TAX_RATE", None)
        if t not in (None, ""):
            tax = float(t)
    except Exception:
        pass
    if commission is None:
        commission = 0.003 if is_mock else 0.00015   # 모의 0.3% / 실전 0.015%
    return commission, tax


COMMISSION_RATE, TAX_RATE = _load_fee_settings()
ROUND_TRIP_COST = 2 * COMMISSION_RATE + TAX_RATE   # 왕복 총비용 (매수수수료+매도수수료+거래세)
logger.info("수수료 설정: 편도 %.3f%% + 세금 %.3f%% -> 왕복 비용 %.3f%%",
            COMMISSION_RATE * 100, TAX_RATE * 100, ROUND_TRIP_COST * 100)


# -------------------------------------------------
# ★ Phase 설정 파일 연동 (이제 수치 수정은 phase_settings.py 에서 합니다)
# -------------------------------------------------
from config.phase_settings import COMMON, PHASE_1A, PHASE_1B, PHASE_2, PHASE_3
from config.phase_settings import EXIT_POLICY, SCORING

# 시간 윈도우
PHASE1_START = time(9, 0)
PHASE1_END   = time(9, 21)
PHASE2_START = time(9, 21)
PHASE2_END   = time(10, 40)
PHASE3_START = time(10, 41)
PHASE3_END   = time(15, 0)

# 진입 조건 (Phase 1A 설정값 사용)
SURGE_THRESHOLD     = PHASE_1A["surge_threshold"]
MA_TOUCH_TOLERANCE  = PHASE_1A["ma_tolerance"]
VOLUME_SURGE_RATIO  = PHASE_1A["volume_surge_ratio"]
VOLUME_LOOKBACK     = 5

# Phase2 이동평균 기간
PHASE2_MA_PERIOD    = PHASE_2["ma_period"]

# 청산 정책 (EXIT_POLICY 딕셔너리에서 가져옴)
TAKE_PROFIT_CAP     = EXIT_POLICY["default"]["take_profit_cap"]
STOP_LOSS_RATE      = EXIT_POLICY["default"]["stop_loss_rate"]
B_STOP_LOSS_FROM_OPEN = EXIT_POLICY["phase3_B"]["stop_loss_from_open"]
TRAIL_ACTIVATE      = EXIT_POLICY["default"]["trail_activate"]
TRAIL_GIVEBACK      = EXIT_POLICY["default"]["trail_giveback"]
# 문자열 "09:30"을 time 객체로 변환
TRAIL_BUY_CUTOFF    = time(*map(int, EXIT_POLICY["default"]["trail_buy_cutoff"].split(":")))
HOLDING_TIMEOUT     = timedelta(minutes=EXIT_POLICY["default"]["holding_timeout_min"])

# 20MA 이탈 청산
EXIT_MA_PERIOD      = EXIT_POLICY["default"]["exit_ma_period"]
EXIT_MA_BREAK_PCT   = EXIT_POLICY["default"]["exit_ma_break_pct"]
EXIT_MA_REFRESH     = timedelta(seconds=30)
EXIT_MA_FETCH_COUNT = 25

# 매도 실패 & 쿨다운 & 워밍업
MAX_SELL_FAIL    = 3
REBUY_COOLDOWN   = timedelta(minutes=COMMON["rebuy_cooldown_min"])
RESTART_WARMUP   = timedelta(seconds=60)
BUY_WARMUP       = timedelta(seconds=COMMON["buy_warmup_sec"])

# 금액 + 슬롯
POSITION_AMOUNT  = COMMON["position_amount"]
MAX_HOLDINGS     = COMMON["max_holdings"]
PHASE1A_MAX_SLOTS = PHASE_1A["max_slots"]
PHASE1B_MAX_SLOTS = PHASE_1B["max_slots"]
PHASE3_MAX_SLOTS  = PHASE_3["max_slots"]

MAX_WATCH_SLOTS = 10
WATCH_TIMEOUT = timedelta(minutes=10)

# 급등 즉시 진입
SURGE_ENTRY_MIN   = 0.03
SURGE_MAX_SLOTS   = 2
SURGE_END = time(9, 30)

# MDD 일손실 차단
DAILY_LOSS_LIMIT = COMMON["mdd_daily_loss_limit"]

def _notify(msg: str):
    if send_telegram is None:
        return
    try:
        send_telegram(msg)
    except Exception as e:
        logger.warning("텔레그램 전송 실패: %s", e)


class StrategyManager:
    def __init__(
        self,
        kiwoom_rest,
        order_manager,
        phase1b_controller=None,
        phase3_controller=None,
        portfolio_optimizer=None,
        now_func=None,
    ):
        self.api = kiwoom_rest
        self.order_manager = order_manager
        self.phase1b = phase1b_controller
        self.phase3 = phase3_controller
        self.optimizer = portfolio_optimizer
        self._now = now_func or datetime.now

        self.holdings: dict[str, dict] = {}
        self.watch_list_today: set[str] = set()
        self.pending: set[str] = set()
        self._stock_names: dict[str, str] = {}
        
        # ▼▼▼ 여기에 추가 ▼▼▼
        # HTS에서 넘어온 1차 합격 종목들의 타점을 기다리는 대기열 {종목코드: 포착시간}
        self.watch_candidates: dict[str, datetime] = {}

        self._sell_fail_count: dict[str, int] = {}
        self._sell_blocked: set[str] = set()
        self._sold_at: dict[str, datetime] = {}
        self._stoploss_blocked: set[str] = set()   # 손절로 나간 종목(당일 재매수 금지)
        self._buy_success_count = 0
# MDD 일손실 차단 (실현손익 기준 -3%)
        self._base_capital = None       # 기준자본 (첫 매수 시도 때 1회 기록)
        self._daily_realized = 0.0      # 오늘 실현손익 누적
        self._risk_tripped = False      # 차단기 발동 여부
        self._risk_date = self._now().date()
        self._kospi_rate = 0.0
        self._kospi_rate_at = None
        # 시장 레짐 (코스피 등락률 기반 threshold 조절)
        self._kospi_rate = 0.0
        self._kospi_rate_at = None   # 마지막 조회 시각

        # 점수 기반 진입 설정.
        # threshold_ratio=1.0 -> 기존 AND와 (거의) 동일. dry-run에서 score_breakdown 로그 본 뒤
        # 0.85 -> 0.75 식으로 단계적으로 낮춰 near-miss 진입 허용(=리스크 노출도 함께 증가).
        self.score_cfg = ScoreConfig(
            surge_target=SURGE_THRESHOLD,
            surge_min=SURGE_ENTRY_MIN,
            ma_tolerance=MA_TOUCH_TOLERANCE,
            volume_target=VOLUME_SURGE_RATIO,
            threshold_ratio=0.75,
        )
        # 9:30 이후 급등 — 추격 위험 구간이라 더 빡빡한 임계값
        self.score_cfg_strict = ScoreConfig(
            surge_target=SURGE_THRESHOLD,
            surge_min=SURGE_ENTRY_MIN,
            ma_tolerance=MA_TOUCH_TOLERANCE,
            volume_target=VOLUME_SURGE_RATIO,
            threshold_ratio=0.90,
        )
# 진입 전략 레지스트리 (등록 순서 = 시간대 겹칠 때 우선순위)
        self.entry_registry = (
            EntryRegistry()
            .register(SurgeStrategy())      # 9:00~10:40 (9:30 이후 strict)
            .register(PullbackStrategy())   # 9:30~10:40
            .register(Phase3Strategy())     # 10:41~15:00
        )
        self._restore_from_db()
        self._last_phase = self.get_current_phase()

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
        """실제 순손익 금액 (수수료/세금 차감)."""
        buy_amt = buy_price * qty
        sell_amt = current_price * qty
        cost = buy_amt * COMMISSION_RATE + sell_amt * (COMMISSION_RATE + TAX_RATE)
        return (sell_amt - buy_amt) - cost

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
                "ma20": None,                 # 20MA 캐시 (미계산)
                "ma20_updated": None,         # 20MA 갱신 시각
                "warmup_until": warmup_until,
            }
        for w in WatchListRepository.find_by_date(self._now().date()):
            self.watch_list_today.add(w["stock_code"])

        logger.info(
            "DB 복원: 보유 %d (1A=%d, 1B=%d, 2=%d, 3=%d) / 워치 %d / 워밍업 %ds",
            len(self.holdings),
            self.count_holdings_by_strategy("1A"),
            self.count_holdings_by_strategy("1B"),
            self.count_holdings_by_strategy("2"),
            self.count_holdings_by_strategy("3"),
            len(self.watch_list_today),
            int(RESTART_WARMUP.total_seconds()),
        )

    # ========================================
    # 二쇨린 ?몄텧 (주기 호출)
    # ========================================
    def tick(self):
        now = self._now()

        cur_phase = self.get_current_phase()
        if cur_phase != self._last_phase:
            # (중략... 전략 전환 로그 찍는 부분)
            self._last_phase = cur_phase

        # ▼▼▼ 여기에 두 번째 복사본(for code in list(self.watch_candidates.keys()): 부분)을 붙여넣으세요 ▼▼▼
        for code in list(self.watch_candidates.keys()):
            # 이미 샀거나 팬딩(주문 중)이면 슬롯에서 비워줌
            if code in self.holdings or code in self.pending:
                del self.watch_candidates[code]
                continue

            try:
                # 1분봉 데이터 70개 호출 (CandleCache가 방어해줌)
                candles = self.api.get_minute_candles(code, interval=1, count=70)
                
                # N자 눌림목 타점 평가!
                is_timing, info = self.evaluate_morning_pullback(candles)
                
                if is_timing:
                    stock_name = self._stock_names.get(code, code)
                    logger.info("🎯 [%s] %s 10MA 눌림목 타점 포착! 매수 실행", code, stock_name)
                    
                    # 매수 실행 (서브 전략 이름 '1N' - N자 눌림목)
                    self._execute_buy(code, stock_name, phase=cur_phase or 1, info=info, sub_strategy="1N")
                    
                    # 매수 후 슬롯 비우기
                    del self.watch_candidates[code]

            except Exception as e:
                logger.warning("[%s] 관찰 종목 타점 평가 중 에러: %s", code, e)
        # ▲▲▲ 여기까지 ▲▲▲

        if self.phase1b and now.time() >= PHASE2_END:
            for code in list(self.phase1b.watched):
                if code not in self.holdings:
                    self.phase1b.stop_watching(code)
        # 보유종목 20MA 캐시 갱신 (청산용)
        self._refresh_exit_ma()

        self.check_timeouts()

    # ========================================
    # 20MA 캐시 갱신 (청산 기준선)
    # ========================================
    def _refresh_exit_ma(self):
        """보유종목별로 20MA를 주기 갱신해 캐시. 분봉 부족/실패 시 None 유지.
        Phase 3-B는 20MA 청산 비활성화이므로 스킵."""
        now = self._now()
        for code, pos in list(self.holdings.items()):
            if code in self._sell_blocked:
                continue
            if pos.get("trigger") == "B":   # B는 20MA 미사용
                continue
            last = pos.get("ma20_updated")
            if last and (now - last) < EXIT_MA_REFRESH:
                continue
            try:
                candles = self.api.get_minute_candles(
                    code, interval=1, count=EXIT_MA_FETCH_COUNT)
                if candles and len(candles) >= EXIT_MA_PERIOD:
                    ma20 = calc_ma(candles, EXIT_MA_PERIOD)
                    if ma20:
                        pos["ma20"] = ma20
                        pos["ma20_updated"] = now
                else:
                    # 분봉 부족(예: 장중 1개 버그) -> 20MA 청산 스킵, 손절/익절캡은 계속 작동
                    logger.debug("[%s] 20MA 갱신 보류: 분봉 %d개",
                                 code, len(candles) if candles else 0)
            except Exception as e:
                logger.warning("[%s] 20MA 갱신 실패: %s", code, e)

    # ========================================
    # Phase 판별
    # ========================================
    def get_current_phase(self) -> Optional[int]:
        t = self._now().time()
        if PHASE1_START <= t < PHASE1_END:
            return 1
        if PHASE2_START <= t < PHASE2_END:
            return 2
        if PHASE3_START <= t < PHASE3_END:
            return 3
        return None
    def _ensure_base_capital(self):
        """기준자본 1회 기록 (주문가능 + 보유 매입원가). 재시작 시에도 근사 유지."""
        if self._base_capital is not None:
            return
        try:
            deposit = float(self.api.get_orderable_amount())
        except Exception:
            return
        holding_cost = sum(p["buy_price"] * p["buy_quantity"] for p in self.holdings.values())
        self._base_capital = deposit + holding_cost
        logger.info("MDD 기준자본 기록: %s원 (주문가능 %s + 보유원가 %s)",
                    f"{self._base_capital:,.0f}", f"{deposit:,.0f}", f"{holding_cost:,.0f}")
    def _risk_daily_reset(self):
        today = self._now().date()
        if today != self._risk_date:
            self._risk_date = today
            self._daily_realized = 0.0
            self._risk_tripped = False
            self._base_capital = None

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
                logger.warning("MDD 일손실 차단 발동: 실현 %s원 (%.2f%%) <= 한도 %.1f%%",
                               f"{self._daily_realized:,.0f}", loss_rate * 100, DAILY_LOSS_LIMIT * 100)
                _notify(f"🛑 MDD 일손실 차단 발동\n"
                        f"실현손익: {self._daily_realized:,.0f}원 ({loss_rate*100:.2f}%)\n"
                        f"기준자본 {self._base_capital:,.0f}원 대비 한도 {DAILY_LOSS_LIMIT*100:.1f}% 초과\n"
                        f"→ 오늘 신규 매수 전면 차단 (보유분 청산은 계속)")
                return False
        return True
    def _refresh_kospi_rate(self):
        """코스피 등락률 1분 캐시. 실패해도 기존값 유지(봇 안 멈춤)."""
        now = self._now()
        if (self._kospi_rate_at is not None
                and (now - self._kospi_rate_at).total_seconds() < 60):
            return
        try:
            rate = self.api.get_index_change_rate("001")
            self._kospi_rate = rate
            self._kospi_rate_at = now
        except Exception:
            pass  # 조회 실패 시 기존 캐시값 유지

    def _market_threshold_adjust(self) -> float:
        """코스피 레짐에 따른 threshold 조절값. +면 타이트, -면 완화."""
        self._refresh_kospi_rate()
        r = self._kospi_rate
        if r >= 1.0:
            return -0.05   # 상승장: 완화
        if r <= -1.0:
            return +0.05   # 하락장: 타이트
        return 0.0
    def _adjusted_cfg(self, base_cfg):
        """코스피 레짐 반영해 threshold_ratio 조절한 cfg 복사본 반환."""
        adj = self._market_threshold_adjust()
        if adj == 0.0:
            return base_cfg
        new_ratio = max(0.5, min(1.0, base_cfg.threshold_ratio + adj))
        return _dc_replace(base_cfg, threshold_ratio=new_ratio)
    def can_buy_more(self) -> bool:
        if not self.risk_can_trade():
            return False
        return len(self.holdings) < MAX_HOLDINGS

    def count_holdings_by_strategy(self, sub: str) -> int:
        return sum(1 for h in self.holdings.values() if h.get("sub_strategy") == sub)

    def can_buy_phase1a(self) -> bool:
        # 눌림목 1A: 9:30~10:40 (급등 구간 끝난 뒤)
        return (self.can_buy_more()
                and self.count_holdings_by_strategy("1A") < PHASE1A_MAX_SLOTS
                and SURGE_END <= self._now().time() < PHASE2_END)
    
    def can_buy_phase1n(self) -> bool:
        # 1N(돌파→눌림목 전환)은 1A와 5MA 눌림목 슬롯 3칸 공유
        used = self.count_holdings_by_strategy("1A") + self.count_holdings_by_strategy("1N")
        return (self.can_buy_more()
                and used < PHASE1A_MAX_SLOTS
                and SURGE_END <= self._now().time() < PHASE2_END)

    def can_buy_phase1b(self) -> bool:
        return (self.can_buy_more()
                and self.count_holdings_by_strategy("1B") < PHASE1B_MAX_SLOTS
                and PHASE1_START <= self._now().time() < PHASE2_END)

    def can_buy_phase3(self) -> bool:
        return (self.can_buy_more()
                and self.count_holdings_by_strategy("3") < PHASE3_MAX_SLOTS
                and PHASE3_START <= self._now().time() < PHASE3_END)

    def can_buy_surge(self) -> bool:
        # 급등 1S: 9:00~10:40 (9:00~9:30 일반 + 9:30~10:40 strict 둘 다 허용)
        return (self.can_buy_more()
                and self.count_holdings_by_strategy("1S") < SURGE_MAX_SLOTS
                and PHASE1_START <= self._now().time() < PHASE2_END)

    # ========================================
    # 쿨다운 / 차단
    # ========================================
    def _is_rebuy_blocked(self, stock_code: str) -> tuple[bool, str]:
        if stock_code in self._sell_blocked:
            return True, "매도 차단 (영구실패)"
        if stock_code in self._stoploss_blocked:
            return True, "손절 종목 당일 재매수 금지"
        if stock_code in self._sold_at:
            elapsed = self._now() - self._sold_at[stock_code]
            if elapsed < REBUY_COOLDOWN:
                remaining = REBUY_COOLDOWN - elapsed
                return True, f"쿨다운 (잔여 {int(remaining.total_seconds())}초)"
        return False, ""

    # ========================================
    # 진입
    # ========================================
    def on_condition_hit(self, stock_code: str, stock_name: str, is_surge: bool = False):
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

        try:
            now_t = self._now().time()
            active = self.entry_registry.route(now_t)
            if not active:
                return

            need_candles = any(s.name in ("surge", "pullback") for s in active)
            candles = []
            if need_candles:
                candles = self.api.get_minute_candles(stock_code, interval=1, count=15)
                if not candles or len(candles) < VOLUME_LOOKBACK + 1:
                    logger.warning("[%s] 분봉 부족 (%d개)", stock_code,
                                   len(candles) if candles else 0)
                    candles = []

            ctx = EntryContext(
                stock_code=stock_code, stock_name=stock_name,
                candles=candles, now_time=now_t, phase=phase,
            )

            # 1) 즉시매수형 전략 평가 (등록 순서 = 우선순위)
            for strat in active:
                ok, info = strat.evaluate(self, ctx)
                if strat.name in ("surge", "pullback"):
                    self._record_watch_list(stock_code, stock_name, phase, info)
                if ok:
                    if strat.can_buy(self):
                        self._execute_buy(stock_code, stock_name, phase, info,
                                          sub_strategy=strat.sub_strategy)
                        return
                    else:
                        logger.info("[%s] %s 조건 OK but 슬롯 부족", stock_code, strat.name)
                elif info.get("reason"):
                    logger.info("[%s] %s %s 미충족: %s",
                                stock_code, stock_name, strat.name, info.get("reason"))

            # 2) 매수 안 됨 → 부수효과(1B/Phase3 감시 시작 등)
            for strat in active:
                strat.on_side_effect(self, ctx)

        except Exception as e:
            logger.exception("[%s] on_condition_hit 실패: %s", stock_code, e)
            SystemEventRepository.log("STRATEGY_ERROR", f"{stock_code}: {e}", "ERROR")
            _notify(f"전략 에러\n{stock_code}: {e}")
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

        if self.phase1b and self.phase1b.is_watching(code):
            state = self.phase1b.on_trade(parsed_trade, now=now)
            if state == ChemulState.READY_TO_BUY:
                self._try_phase1b_buy(code, now)

        if self.phase3 and self.phase3.is_watching(code):
            from core.strategy.phase3_controller import Phase3State
            new_state = self.phase3.on_trade(parsed_trade)
            if new_state == Phase3State.READY_TO_BUY:
                self._try_phase3_buy(code)

    def on_orderbook(self, parsed_orderbook: dict, now: float = None):
        code = parsed_orderbook.get("stock_code")
        if not code:
            return
        if self.phase1b and self.phase1b.is_watching(code):
            state = self.phase1b.on_orderbook(parsed_orderbook, now=now)
            if state == ChemulState.READY_TO_BUY:
                self._try_phase1b_buy(code, now)
        # Phase3 B 트리거용: 호가 잔량 비율만 갱신 (매수 판정은 on_trade에서)
        if self.phase3 and self.phase3.is_watching(code):
            try:
                self.phase3.on_orderbook(parsed_orderbook)
            except Exception:
                logger.exception("[%s] phase3 on_orderbook 예외", code)

    def _try_phase1b_buy(self, stock_code: str, now: float = None):
        if not self.can_buy_phase1b():
            logger.info("[%s] Phase 1B READY but 슬롯 부족", stock_code)
            return
        if stock_code in self.holdings or stock_code in self.pending:
            return
        blocked, reason = self._is_rebuy_blocked(stock_code)
        if blocked:
            logger.info("[%s] Phase 1B 매수 차단: %s", stock_code, reason)
            return

        current_price = self.phase1b.trade_flow.get_latest_price(stock_code)
        if not current_price:
            logger.warning("[%s] Phase 1B 매수 시도 but 가격 없음", stock_code)
            return

        stock_name = self._stock_names.get(stock_code, stock_code)
        info = {"current_price": current_price, "volume_ratio": 0.0}
        self._execute_buy(stock_code, stock_name, phase=1, info=info, sub_strategy="1B")
        self.phase1b.stop_watching(stock_code)

    def _try_phase3_buy(self, stock_code: str):
        if not self.can_buy_phase3():
            logger.info("[%s] Phase 3 READY but 슬롯 부족", stock_code)
            self.phase3.stop_watching(stock_code)
            return
        if stock_code in self.holdings or stock_code in self.pending:
            return
        blocked, reason = self._is_rebuy_blocked(stock_code)
        if blocked:
            logger.info("[%s] Phase 3 매수 차단: %s", stock_code, reason)
            self.phase3.stop_watching(stock_code)
            return

        try:
            candles = self.api.get_minute_candles(stock_code, interval=1, count=30)
        except Exception:
            candles = None

        if not candles or not candles[0].get("close"):
            logger.warning("[%s] Phase 3 매수 시도 but 분봉 없음", stock_code)
            self.phase3.stop_watching(stock_code)
            return
        current_price = candles[0]["close"]

        # ── 점수 게이트 (이중 게이트: FSM OK + 오후 점수 통과해야 매수) ──
        ok, score_info = self.evaluate_phase3_rank(candles, stock_code)
        if not ok:
            # 점수 미달: 이번 매수만 스킵, 감시는 유지(다음 FSM 신호 때 재평가)
            logger.info("[%s] Phase 3 FSM OK but 점수 미달, 매수 스킵: %s",
                        stock_code, score_info.get("reason"))
            return

        # 트리거 정보(A/B) + 시가 받아서 score_info에 주입
        trig_info = self.phase3.get_trigger_info(stock_code)
        trigger = trig_info.get("trigger")
        opening_price = trig_info.get("opening_price", 0.0)
        if trigger == "B" and opening_price <= 0:
            logger.warning("[%s] Phase 3-B 매수 보류: 시가 미확보", stock_code)
            self.phase3.stop_watching(stock_code)
            return
        score_info["trigger"] = trigger
        score_info["opening_price"] = opening_price
        logger.info("[%s] Phase 3-%s 매수 진행 (시가=%.0f)",
                    stock_code, trigger or "?", opening_price)

        stock_name = self._stock_names.get(stock_code, stock_code)
        self._execute_buy(stock_code, stock_name, phase=3, info=score_info, sub_strategy="3")
        self.phase3.stop_watching(stock_code)

    # ========================================
    # 진입 평가 (점수 기반 — scoring.py 위임)
    #   반환 계약/‌info 키는 기존과 동일. threshold_ratio로 강도 조절.
    # ========================================
    def evaluate_phase1(self, candles, stock_code):
        return score_phase1(candles, self._volume_ratio(candles),
                            self._current_strength(stock_code), self.score_cfg)

    def evaluate_surge(self, candles, stock_code, cfg=None):
        base = cfg or self.score_cfg
        return score_surge(candles, self._volume_ratio(candles),
                           self._current_strength(stock_code), self._adjusted_cfg(base))
    
    def evaluate_phase2(self, candles, stock_code):
        if len(candles) < PHASE2_MA_PERIOD:
            return False, {"reason": f"분봉 {len(candles)}개 ({PHASE2_MA_PERIOD}MA 불가)"}
        return score_phase2(candles, self._volume_ratio(candles), PHASE2_MA_PERIOD,
                            self._current_strength(stock_code), self.score_cfg)
    
    def evaluate_pullback(self, candles, stock_code):
        return score_pullback(candles, self._volume_ratio(candles),
                              self._current_strength(stock_code), self._adjusted_cfg(self.score_cfg))

    def evaluate_phase3_rank(self, candles, stock_code):
        return score_phase3_rank(candles, self._volume_ratio(candles),
                                self._current_strength(stock_code), self._adjusted_cfg(self.score_cfg))

    def _current_strength(self, stock_code):
        # 체결강도 조회. phase1b 없거나 실패/데이터 없으면 중립값 100 반환.
        if stock_code and self.phase1b and getattr(self.phase1b, "trade_flow", None):
            try:
                return self.phase1b.trade_flow.compute_strength(stock_code, window_sec=10)
            except Exception:
                pass
        return 100.0
    @staticmethod
    def _volume_ratio(candles: list[dict]) -> float:
        cur_vol = candles[0]["volume"]
        prev = candles[1:1 + VOLUME_LOOKBACK]
        if not prev:
            return 0.0
        avg = sum(c["volume"] for c in prev) / len(prev)
        return cur_vol / avg if avg > 0 else 0.0

        # ========================================
    # 매수 실행
    # ========================================
    def _resolve_position_amount(self, stock_code: str, sub_strategy: str) -> tuple[int, Optional[dict]]:
        if self.optimizer is None:
            return POSITION_AMOUNT, None
        try:
            opt_info = self.optimizer.calculate_position_amount(stock_code, sub_strategy)
            amount = int(opt_info.get("amount", 0))
            if amount <= 0:
                return POSITION_AMOUNT, None
            return amount, opt_info
        except Exception:
            logger.exception(f"[{stock_code}] 비중 계산 실패, fallback")
            return POSITION_AMOUNT, None

    def _execute_buy(self, stock_code, stock_name, phase, info, sub_strategy):
        current_price = info["current_price"]
        sc = info.get("score")
        if sc is not None:
            logger.info("[%s] %s 매수평가 통과 | score=%.2f/%.2f | %s",
                        stock_code, stock_name, sc, info.get("score_threshold", 0),
                        info.get("score_breakdown", ""))

        position_amount, opt_info = self._resolve_position_amount(stock_code, sub_strategy)
        quantity = int(position_amount // current_price)
        if quantity < 1:
            logger.warning("[%s] %s 수량 0 -> skip", stock_code, stock_name)
            return

        if opt_info:
            logger.info("[%s] 동적 비중 %.2fx -> %s원", stock_code,
                        opt_info.get("final_weight", 1.0), f"{position_amount:,}")

        self.pending.add(stock_code)
        try:
            ma_val = info.get("ma5") or 0
            if sub_strategy == "1B":
                entry_reason = f"Phase 1B 체결강도 (현재가 {current_price:,})"
            elif sub_strategy == "3":
                entry_reason = f"Phase 3 단조증가 1분유지 (현재가 {current_price:,})"
            elif sub_strategy == "1S":
                entry_reason = (f"급등 진입 | 시초가+{info.get('surge_rate', 0)*100:.2f}% "
                                f"| vol x{info.get('volume_ratio', 0):.2f} (현재가 {current_price:,})")
            else:
                # 1A=5MA, 2=30MA (PHASE2_MA_PERIOD)
                ma_label = "5" if sub_strategy == "1A" else str(PHASE2_MA_PERIOD)
                entry_reason = (f"Phase{sub_strategy} | "
                                f"MA{ma_label}={ma_val:,.0f} "
                                f"| vol x{info.get('volume_ratio', 0):.2f}")
                if sub_strategy == "1A":
                    entry_reason += f" | 시초가+{info.get('surge_rate', 0)*100:.2f}%"
            if opt_info:
                entry_reason += f" | 비중x{opt_info.get('final_weight', 1.0):.2f}"

            result = self.order_manager.buy(stock_code, quantity, price=0)
            if not result or not result.get("success"):
                err = (result or {}).get("error", "unknown")
                logger.error("[%s] 매수 실패: %s", stock_code, err)
                SystemEventRepository.log("ORDER_FAIL", f"BUY {stock_code}: {err}", "ERROR")
                _notify(f"매수 실패\n{stock_code} {stock_name}\n사유: {err}")
                return

            trade_id = TradeRepository.insert_buy(
                stock_code=stock_code, stock_name=stock_name,
                buy_price=current_price, buy_quantity=quantity,
                strategy_phase=phase, sub_strategy=sub_strategy,
                entry_reason=entry_reason,
            )

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
                "trigger": info.get("trigger"),                       # Phase3 A/B (Phase3만 세팅)
                "opening_price": info.get("opening_price", 0.0),      # B 손절 기준점
                "position_weight": (opt_info or {}).get("final_weight", 1.0),
                "warmup_until": self._now() + BUY_WARMUP,
            }
        
            self._sold_at.pop(stock_code, None)
            self._buy_success_count += 1

            logger.info("BUY [%s] %s %d주 @ %s원 (%s) = %s원 | 워밍업 %ds",
                        stock_code, stock_name, quantity, f"{current_price:,}",
                        sub_strategy, f"{current_price * quantity:,}",
                        int(BUY_WARMUP.total_seconds()))
            SystemEventRepository.log("BUY",
                f"{stock_code} {stock_name} {quantity}주 @ {current_price:,}원 [{sub_strategy}]", "INFO")
            weight_str = f" (비중 {opt_info['final_weight']:.2f}x)" if opt_info else ""
            _notify(f"매수 체결 [{sub_strategy}]\n종목: {stock_name} ({stock_code})\n"
                    f"수량: {quantity}주 @ {current_price:,}원{weight_str}\n"
                    f"금액: {current_price * quantity:,}원\n전략: {entry_reason}")
            self._mark_watch_bought(stock_code)
        finally:
            self.pending.discard(stock_code)

    # ========================================
    # 워치리스트
    # ========================================
    def _record_watch_list(self, stock_code, stock_name, phase, info):
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
            WatchListRepository.add(stock_code=stock_code, stock_name=stock_name, phase=phase, **extra)
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
        if stock_code in self._sell_blocked:
            return

        buy_price = pos["buy_price"]
        if not buy_price:
            return

        if current_price > pos["highest_price"]:
            pos["highest_price"] = current_price

        warmup_until = pos.get("warmup_until")
        if warmup_until and self._now() < warmup_until:
            return

        gross_rate = self._gross_rate(buy_price, current_price)   # 가격 기준
        net_rate   = gross_rate - ROUND_TRIP_COST                 # 순익 기준
        trigger = pos.get("trigger")                              # Phase3 "A"/"B"/None

        exit_reason = None

        # 1) 손절: B는 시가-1%, 그 외는 가격-3%
        if trigger == "B":
            opening = pos.get("opening_price", 0.0)
            if opening > 0:
                stop_line = opening * (1 + B_STOP_LOSS_FROM_OPEN)  # opening * 0.99
                if current_price <= stop_line:
                    drop = (current_price - opening) / opening * 100
                    exit_reason = (f"B 손절 시가{B_STOP_LOSS_FROM_OPEN*100:.1f}% "
                                   f"(시가 {opening:,.0f} → {current_price:,}, "
                                   f"{drop:+.2f}%, 순 {net_rate*100:+.2f}%)")
        else:
            if gross_rate <= STOP_LOSS_RATE:
                exit_reason = f"손절 가격{gross_rate*100:.2f}% (순 {net_rate*100:.2f}%)"

        # 2) 익절
        if exit_reason is None:
            buy_t = pos["buy_time"].time() if hasattr(pos["buy_time"], "time") else None

            if trigger in ("A", "B"):
                # Phase 3 (A·B 둘 다 트레일링)
                highest = pos.get("highest_price", buy_price)
                peak_net = self._net_rate(buy_price, highest)
                if peak_net >= TRAIL_ACTIVATE:
                    pos["trail_armed"] = True
                if pos.get("trail_armed"):
                    trail_line = highest * (1 - TRAIL_GIVEBACK)
                    if current_price <= trail_line:
                        give = (current_price - highest) / highest * 100
                        exit_reason = (f"트레일링 고점-{TRAIL_GIVEBACK*100:.0f}% "
                                       f"({give:+.2f}%, 순 {net_rate*100:+.2f}%) [{trigger}]")
            else:
                # 기존 1A/1B/2/1S 등: 매수 시각으로 트레일링 or 익절캡
                use_trailing = (buy_t is not None and buy_t < TRAIL_BUY_CUTOFF)
                if use_trailing:
                    highest = pos.get("highest_price", buy_price)
                    peak_net = self._net_rate(buy_price, highest)
                    if peak_net >= TRAIL_ACTIVATE:
                        pos["trail_armed"] = True
                    if pos.get("trail_armed"):
                        trail_line = highest * (1 - TRAIL_GIVEBACK)
                        if current_price <= trail_line:
                            give = (current_price - highest) / highest * 100
                            exit_reason = (f"트레일링 고점-{TRAIL_GIVEBACK*100:.0f}% "
                                           f"({give:+.2f}%, 순 {net_rate*100:+.2f}%)")
                else:
                    if net_rate >= TAKE_PROFIT_CAP:
                        exit_reason = f"익절 캡 순+{net_rate*100:.2f}% (가격 +{gross_rate*100:.2f}%)"

            # 3) 20MA 이탈 (B는 비활성화)
            if exit_reason is None and trigger != "B":
                ma20 = pos.get("ma20")
                if ma20:
                    break_line = ma20 * (1 - EXIT_MA_BREAK_PCT)
                    if current_price < break_line:
                        below_pct = (current_price - ma20) / ma20 * 100
                        exit_reason = (f"20MA 이탈 ({below_pct:+.2f}%, "
                                       f"순 {net_rate*100:+.2f}%)")

            # 3) 20MA 이탈 (트레일링/익절캡 미발동 시 공통 안전망)
            if exit_reason is None:
                ma20 = pos.get("ma20")
                if ma20:
                    break_line = ma20 * (1 - EXIT_MA_BREAK_PCT)
                    if current_price < break_line:
                        below_pct = (current_price - ma20) / ma20 * 100
                        exit_reason = (f"20MA 이탈 ({below_pct:+.2f}%, "
                                       f"순 {net_rate*100:+.2f}%)")

        # 4) 시간정리 30분 (B는 비활성화)
        if exit_reason is None and trigger != "B":
            if self._now() - pos["buy_time"] >= HOLDING_TIMEOUT:
                exit_reason = f"시간정리 30분 (순 {net_rate*100:+.2f}%)"

        if exit_reason:
            self._execute_sell(stock_code, current_price, exit_reason)

    def _execute_sell(self, stock_code, current_price, exit_reason):
        pos = self.holdings.get(stock_code)
        if not pos or stock_code in self.pending:
            return
        if stock_code in self._sell_blocked:
            return

        logger.info("청산 트리거 [%s] %s | 사유: %s | 현재가 %s",
                    stock_code, pos.get("stock_name", ""), exit_reason, f"{current_price:,}")

        self.pending.add(stock_code)
        try:
            quantity = pos["buy_quantity"]
            result = self.order_manager.sell(stock_code, quantity, price=0)

            if not result or not result.get("success"):
                err = (result or {}).get("error", "unknown")
                cnt = self._sell_fail_count.get(stock_code, 0) + 1
                self._sell_fail_count[stock_code] = cnt

                logger.error("[%s] 매도 실패 (%d/%d): %s", stock_code, cnt, MAX_SELL_FAIL, err)
                SystemEventRepository.log("ORDER_FAIL",
                    f"SELL {stock_code}: {err} ({cnt}/{MAX_SELL_FAIL})", "ERROR")

                if cnt >= MAX_SELL_FAIL:
                    self._sell_blocked.add(stock_code)
                    stock_name = pos.get("stock_name", stock_code)
                    self.holdings.pop(stock_code, None)
                    logger.warning("[%s] %s 매도 %d회 실패 -> 차단", stock_code, stock_name, cnt)
                    SystemEventRepository.log("SELL_BLOCKED",
                        f"{stock_code} 매도 {cnt}회 실패 -> 차단", "WARNING")
                    _notify(f"매도 차단\n{stock_name} ({stock_code})\n"
                            f"매도 {cnt}회 실패 -> 수동 확인 필요")
                else:
                    _notify(f"매도 실패 ({cnt}/{MAX_SELL_FAIL})\n"
                            f"{pos.get('stock_name', stock_code)} ({stock_code})\n사유: {err}")
                return

            self._sell_fail_count.pop(stock_code, None)
            self._sold_at[stock_code] = self._now()
            self._sold_at[stock_code] = self._now()
            # B안: 손절로 나간 종목은 당일 재매수 금지 (익절 종목만 3분 후 재매수)
            if exit_reason and exit_reason.startswith("손절"):
                self._stoploss_blocked.add(stock_code)
                logger.info("[%s] 손절 청산 → 당일 재매수 차단", stock_code)

            TradeRepository.update_sell(
                trade_id=pos["trade_id"], sell_price=current_price,
                sell_quantity=quantity, exit_reason=exit_reason,
            )

            # 순손익 (수수료/세금 차감)
            gross_profit = (current_price - pos["buy_price"]) * quantity
            net_profit = self._net_profit(pos["buy_price"], current_price, quantity)
            net_profit = self._net_profit(pos["buy_price"], current_price, quantity)
            self._daily_realized += net_profit          # ← 이 줄 추가 (MDD 누적)
            gross_rate = self._gross_rate(pos["buy_price"], current_price) * 100
            net_rate = self._net_rate(pos["buy_price"], current_price) * 100
            stock_name = pos["stock_name"]
            sub = pos.get("sub_strategy", "?")
            del self.holdings[stock_code]

            logger.info("SELL [%s] %s %d주 @ %s원 -> %s | 순손익 %s원 (순 %.2f%%, 가격 %.2f%%) [%s]",
                        stock_code, stock_name, quantity, f"{current_price:,}", exit_reason,
                        f"{net_profit:+,.0f}", net_rate, gross_rate, sub)
            SystemEventRepository.log("SELL",
                f"{stock_code} {stock_name} {quantity}주 @ {current_price:,}원 [{sub}] | "
                f"{exit_reason} | 순손익 {net_profit:+,.0f}원", "INFO")
            emoji = "+" if net_profit > 0 else "-"
            _notify(f"매도 체결 [{sub}] ({emoji})\n종목: {stock_name} ({stock_code})\n"
                    f"수량: {quantity}주 @ {current_price:,}원\n"
                    f"순손익: {net_profit:+,.0f}원 (순 {net_rate:+.2f}%, 가격 {gross_rate:+.2f}%)\n"
                    f"사유: {exit_reason}\n재매수 차단: {int(REBUY_COOLDOWN.total_seconds()/60)}분")
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
            if code in self._sell_blocked:
                continue
            if pos.get("trigger") == "B":   # B는 시간정리 면제
                continue
            try:
                candles = self.api.get_minute_candles(code, interval=1, count=1)
                if candles:
                    self._execute_sell(code, candles[0]["close"], "시간정리 30분")
            except Exception as e:
                logger.exception("[%s] 타임아웃 청산 실패: %s", code, e)
                # ========================================
