# -------------------------------------------------
from config.phase_settings import COMMON, PHASE_1A, PHASE_1B, PHASE_2, PHASE_3
from config.phase_settings import EXIT_POLICY, SCORING
print("✅ [시스템 준비] 기본 수수료 0.2% 세팅 대기 완료 (매매 발생 시 적용)")
# 시간 윈도우
PHASE1_START = time(9, 0)
PHASE1_END = time(9, 21)
PHASE2_START = time(9, 21)
PHASE2_END = time(10, 40)
PHASE3_START = time(10, 41)
PHASE3_END = time(15, 0)

# 진입 조건 (Phase 1A 설정값 사용)
SURGE_THRESHOLD = PHASE_1A["surge_threshold"]
MA_TOUCH_TOLERANCE = PHASE_1A["ma_tolerance"]
VOLUME_SURGE_RATIO = PHASE_1A["volume_surge_ratio"]
VOLUME_LOOKBACK = 5

# Phase2 이동평균 기간
PHASE2_MA_PERIOD = PHASE_2["ma_period"]

# 청산 정책 (EXIT_POLICY 딕셔너리에서 가져옴)
TAKE_PROFIT_CAP = EXIT_POLICY["default"]["take_profit_cap"]
STOP_LOSS_RATE = EXIT_POLICY["default"]["stop_loss_rate"]
B_STOP_LOSS_FROM_OPEN = EXIT_POLICY["phase3_B"]["stop_loss_from_open"]
TRAIL_ACTIVATE = EXIT_POLICY["default"]["trail_activate"]
TRAIL_GIVEBACK = EXIT_POLICY["default"]["trail_giveback"]
PHASE1A_CUT_OFF_TIME = time(9, 40)  # 9시 40분 장초/장중 구분 시간
# 문자열 "09:30"을 time 객체로 변환
TRAIL_BUY_CUTOFF = time(
    *map(int, EXIT_POLICY["default"]["trail_buy_cutoff"].split(":"))
)
HOLDING_TIMEOUT = timedelta(minutes=EXIT_POLICY["default"]["holding_timeout_min"])

# 매도 실패 & 쿨다운 & 워밍업
MAX_SELL_FAIL = 3
REBUY_COOLDOWN = timedelta(minutes=COMMON["rebuy_cooldown_min"])
RESTART_WARMUP = timedelta(seconds=60)
BUY_WARMUP = timedelta(seconds=COMMON["buy_warmup_sec"])

# 금액 + 슬롯
POSITION_AMOUNT = COMMON["position_amount"]
MAX_HOLDINGS = COMMON["max_holdings"]
PHASE1A_MAX_SLOTS = PHASE_1A["max_slots"]
PHASE1B_MAX_SLOTS = PHASE_1B["max_slots"]
PHASE3_MAX_SLOTS = PHASE_3["max_slots"]

MAX_WATCH_SLOTS = 10
WATCH_TIMEOUT = timedelta(minutes=10)

# 급등 즉시 진입
SURGE_ENTRY_MIN = 0.03
SURGE_MAX_SLOTS = 2
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

        # 🚨 주도테마 초기화 (장 시작 전 데이터 미리 로드)
        self.theme_mgr = ThemeManager()
        self.theme_mgr.fetch_themes_from_github()

        self.sell_fail_count: dict[str, int] = {}
        self.sell_blocked: set[str] = set()
        self.sold_at: dict[str, datetime] = {}
        # ▲▲▲ 여기까지 ▲▲▲
        self._stoploss_blocked: set[str] = set()  # 손절로 나간 종목(당일 재매수 금지)
        self._buy_success_count = 0
        # MDD 일손실 차단 (실현손익 기준 -3%)
        self._base_capital = None  # 기준자본 (첫 매수 시도 때 1회 기록)
        self._daily_realized = 0.0  # 오늘 실현손익 누적
        self._risk_tripped = False  # 차단기 발동 여부
        self._risk_date = self._now().date()
        self._kospi_rate = 0.0
        self._kospi_rate_at = None
        # 시장 레짐 (코스피 등락률 기반 threshold 조절)
        self._kospi_rate = 0.0
        self._kospi_rate_at = None  # 마지막 조회 시각

        # 점수 기반 진입 설정.
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
            .register(SurgeStrategy())  # 9:00~10:40 (9:30 이후 strict)
            .register(PullbackStrategy())  # 9:30~10:40
            .register(Phase3Strategy())  # 10:41~15:00
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
                "ma20": None,  # 20MA 캐시 (미계산)
                "ma20_updated": None,  # 20MA 갱신 시각
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
    # 주기 루프 (주기 호출)
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
                # 1분봉 데이터 70개 호출 (병합 메서드 사용)
                candles = self._get_merged_candles(code, interval=1, count=70)

                # N자 눌림목 타점 평가!
                is_timing, info = self.evaluate_morning_pullback(candles)

                if is_timing:
                    stock_name = self._stock_names.get(code, code)
                    logger.info(
                        "🎯 [%s] %s 10MA 눌림목 타점 포착! 매수 실행", code, stock_name
                    )

                    # 매수 실행 (서브 전략 이름 '1N' - N자 눌림목)
                    self._execute_buy(
                        code,
                        stock_name,
                        phase=cur_phase or 1,
                        info=info,
                        sub_strategy="1N",
                    )

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
        # self._refresh_exit_ma()

        self.check_timeouts()

    # ========================================
    # 20MA 캐시 갱신 (청산 기준선)
    # ========================================

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

    def _refresh_kospi_rate(self):
        """코스피 등락률 1분 캐시. 실패해도 기존값 유지(봇 안 멈춤)."""
        now = self._now()
        if (
            self._kospi_rate_at is not None
            and (now - self._kospi_rate_at).total_seconds() < 60
        ):
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
            return -0.05  # 상승장: 완화
        if r <= -1.0:
            return +0.05  # 하락장: 타이트
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
        return (
            self.can_buy_more()
            and self.count_holdings_by_strategy("1A") < PHASE1A_MAX_SLOTS
            and SURGE_END <= self._now().time() < PHASE2_END
        )

    def can_buy_phase1n(self) -> bool:
        # 1N(돌파→눌림목 전환)은 1A와 5MA 눌림목 슬롯 3칸 공유
        used = self.count_holdings_by_strategy("1A") + self.count_holdings_by_strategy(
            "1N"
        )
        return (
            self.can_buy_more()
            and used < PHASE1A_MAX_SLOTS
            and SURGE_END <= self._now().time() < PHASE2_END
        )

    def can_buy_phase1b(self) -> bool:
        return (
            self.can_buy_more()
            and self.count_holdings_by_strategy("1B") < PHASE1B_MAX_SLOTS
            and PHASE1_START <= self._now().time() < PHASE2_END
        )

    def can_buy_phase3(self) -> bool:
        return (
            self.can_buy_more()
            and self.count_holdings_by_strategy("3") < PHASE3_MAX_SLOTS
            and PHASE3_START <= self._now().time() < PHASE3_END
        )

    def can_buy_surge(self) -> bool:
        # 급등 1S: 9:00~10:40 (9:00~9:30 일반 + 9:30~10:40 strict 둘 다 허용)
        return (
            self.can_buy_more()
            and self.count_holdings_by_strategy("1S") < SURGE_MAX_SLOTS
            and PHASE1_START <= self._now().time() < PHASE2_END
        )

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
    def on_condition_hit(
        self, stock_code: str, stock_name: str, is_surge: bool = False
    ):
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
                # 병합 메서드 사용
                candles = self._get_merged_candles(stock_code, interval=1, count=15)
                if not candles or len(candles) < VOLUME_LOOKBACK + 1:
                    logger.warning(
                        "[%s] 분봉 부족 (%d개)",
                        stock_code,
                        len(candles) if candles else 0,
                    )
                    candles = []

            ctx = EntryContext(
                stock_code=stock_code,
                stock_name=stock_name,
                candles=candles,
                now_time=now_t,
                phase=phase,
            )

            # ==========================================
            # [우선순위 1] 신규 체결강도 & 30봉 신고가 필터링
            # ==========================================
            now = self._now().time()
            if phase == 1:
                try:
                    c = candles[0] if candles else {}
                    current_price = int(c.get('close', 0))
                    open_price = int(c.get('open', 0))

                    if current_price > 0:
                        ok, info = self.evaluate_new_intensity_strategy(
                            stock_code, candles, current_price, open_price
                        )
                        self._record_watch_list(stock_code, stock_name, phase, info)

                        if ok:
                            if self.can_buy_phase1a():
                                self._execute_buy(stock_code, stock_name, phase, info, sub_strategy="1A")
                                return  # 신규 로직 매수 성공 시 종료
                except Exception as e:
                    logger.debug(f"[{stock_code}] 신규 로직 예외: {e}")

            # ==========================================
            # [우선순위 2] 기존 전략 평가 (surge는 무시, pullback은 유지)
            # ==========================================
            for strat in active:
                # 🚨 surge 전략은 무시하고 건너뜀
                if strat.name == "surge":
                    continue

                ok, info = strat.evaluate(self, ctx)
                if strat.name == "pullback":
                    self._record_watch_list(stock_code, stock_name, phase, info)

                if ok:
                    if strat.can_buy(self):
                        self._execute_buy(
                            stock_code,
                            stock_name,
                            phase,
                            info,
                            sub_strategy=strat.sub_strategy,
                        )
                        return

            # 1) 즉시매수형 전략 평가 (등록 순서 = 우선순위)
            for strat in active:
                ok, info = strat.evaluate(self, ctx)
                if strat.name in ("surge", "pullback"):
                    self._record_watch_list(stock_code, stock_name, phase, info)
                if ok:
                    if strat.can_buy(self):
                        self._execute_buy(
                            stock_code,
                            stock_name,
                            phase,
                            info,
                            sub_strategy=strat.sub_strategy,
                        )
                        return
                    else:
                        logger.info(
                            "[%s] %s 조건 OK but 슬롯 부족", stock_code, strat.name
                        )
                elif info.get("reason"):
                    logger.info(
                        "[%s] %s %s 미충족: %s",
                        stock_code,
                        stock_name,
                        strat.name,
                        info.get("reason"),
                    )

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
            # 병합 메서드 사용
            candles = self._get_merged_candles(stock_code, interval=1, count=30)
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
            logger.info(
                "[%s] Phase 3 FSM OK but 점수 미달, 매수 스킵: %s",
                stock_code,
                score_info.get("reason"),
            )
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
        logger.info(
            "[%s] Phase 3-%s 매수 진행 (시가=%.0f)",
            stock_code,
            trigger or "?",
            opening_price,
        )

        stock_name = self._stock_names.get(stock_code, stock_code)
        self._execute_buy(
            stock_code, stock_name, phase=3, info=score_info, sub_strategy="3"
        )
        self.phase3.stop_watching(stock_code)

    # ========================================
