"""
자동매매 봇 진입점
실행: python main.py
종료: Ctrl+C
"""
import asyncio
import time
from datetime import datetime, time as dtime

from api.auth import get_access_token, send_telegram
from api.kiwoom_rest import KiwoomREST
from api.kiwoom_ws import KiwoomWS
from core.order_manager import OrderManager, FORCE_CLOSE_TIME, MAX_POSITIONS
from core.phase1b_controller import Phase1BController
from core.strategy_manager import StrategyManager
from core.strategy.portfolio_optimizer import PortfolioOptimizer
from config import settings
from utils.logger import logger


POSITION_CHECK_INTERVAL = 30
SYNC_INTERVAL = 60
STATUS_REPORT_INTERVAL = 1800
TOKEN_REFRESH_INTERVAL = 23 * 3600
SIGNAL_WATCHDOG_INTERVAL = 300
SIGNAL_TIMEOUT = 1800
STRATEGY_TICK_INTERVAL = 10
SNAPSHOT_STAGGER_SEC = 0.5  # 스냅샷 종목 처리 간격
POLL_INTERVAL_SEC = 20      # 조건검색 주기 폴링 간격(초)


def _extract_stock_name(raw: dict, stock_code: str) -> str:
    if not isinstance(raw, dict):
        return stock_code
    for key in ("302", "hng_name", "stock_name", "name", "kor_name", "jongmok"):
        v = raw.get(key)
        if v and isinstance(v, str) and v.strip():
            return v.strip()
    return stock_code


class TradingBot:
    def __init__(self):
        self.token: str = ""
        self.rest: KiwoomREST = None
        self.ws: KiwoomWS = None
        self.order_mgr: OrderManager = None
        self.phase1b_ctrl: Phase1BController = None
        self.optimizer: PortfolioOptimizer = None
        self.strategy_mgr: StrategyManager = None
        self._stop = False

        self._signal_stats = {"insert": 0, "delete": 0, "buy_attempted": 0, "snapshot": 0, "poll": 0}
        self._subscribed: set[str] = set()
        self._sub_buffer: list[str] = []          # 0B/0D 구독 대기 버퍼 (3개씩 배치)
        self._sub_buffer_lock = asyncio.Lock()
        self._last_buffer_add = 0.0               # 마지막 버퍼 추가 시각(플러시 판단용)
        self._raw_keys_logged = False
        self.surge_seqs: set[str] = set()   # 급등 즉시매수 대상 조건 seq
        self._known_hits: dict[str, set[str]] = {}  # cond_seq -> 이미 본 종목 (폴링 diff용)

    async def setup(self):
        logger.info("=" * 60)
        logger.info("자동매매 봇 시작")
        logger.info(f"   모드: {'모의투자' if settings.IS_MOCK else '실전'}")
        logger.info(f"   조건식: {settings.CONDITION_NAMES}")
        logger.info("=" * 60)

        self.token = get_access_token()
        if not self.token:
            raise RuntimeError("토큰 발급 실패")

        self.rest = KiwoomREST(self.token, is_mock=settings.IS_MOCK)
        self.order_mgr = OrderManager(self.rest)
        self.order_mgr.sync_positions_from_server()

        self.phase1b_ctrl = Phase1BController()
        self.optimizer = PortfolioOptimizer(rest_api=self.rest)
        from core.strategy.phase3_controller import Phase3Controller
        self.phase3_ctrl = Phase3Controller()
        self.strategy_mgr = StrategyManager(
            kiwoom_rest=self.rest,
            order_manager=self.order_mgr,
            phase1b_controller=self.phase1b_ctrl,
            phase3_controller=self.phase3_ctrl,
            portfolio_optimizer=self.optimizer,
        )

        self.ws = KiwoomWS(
            self.token,
            is_mock=settings.IS_MOCK,
            on_signal=self._on_signal,
            on_trade=self._on_trade,
            on_orderbook=self._on_orderbook,
        )
        await self.ws.connect()
        await self.ws.fetch_condition_list()
        self._resolve_surge_seqs()
        await self._subscribe_conditions()

        # ★ 신규: 현재 진입 종목 스냅샷 처리
        await self._process_initial_snapshot()

        deposit = self.rest.get_orderable_amount()
        msg = (f"자동매매 봇 시작\n"
               f"모드: {'모의투자' if settings.IS_MOCK else '실전'}\n"
               f"조건식: {', '.join(settings.CONDITION_NAMES)}\n"
               f"주문가능: {deposit:,}원\n"
               f"보유: {len(self.strategy_mgr.holdings)}종목 "
               f"(1A={self.strategy_mgr.count_holdings_by_strategy('1A')}, "
               f"1B={self.strategy_mgr.count_holdings_by_strategy('1B')}, "
               f"2={self.strategy_mgr.count_holdings_by_strategy('2')}, "
                f"3={self.strategy_mgr.count_holdings_by_strategy('3')})\n"
               f"동적 비중: 활성 (Kelly+Volatility)\n"
               f"초기 스냅샷: {self._signal_stats['snapshot']}종목 처리")
        send_telegram(msg, target="signal")
        logger.info(msg)

    def _resolve_surge_seqs(self):
        """SURGE_CONDITION_NAMES(이름) → 현재 조건 seq 집합으로 해석."""
        cond_map = self.ws.condition_map
        name_to_seq = {name: seq for seq, name in cond_map.items()}
        surge_names = getattr(settings, "SURGE_CONDITION_NAMES", []) or []
        self.surge_seqs = {name_to_seq[n] for n in surge_names if n in name_to_seq}
        missing = [n for n in surge_names if n not in name_to_seq]
        logger.info(f"⚡ 급등 조건 seq: {sorted(self.surge_seqs)} (대상: {surge_names})")
        if missing:
            logger.warning(f"⚠️ 급등 조건 이름 매칭 실패: {missing} (영웅문 이름과 대조)")
  
    async def _subscribe_conditions(self):
        cond_map = self.ws.condition_map
        name_to_seq = {name: seq for seq, name in cond_map.items()}

        for name in settings.CONDITION_NAMES:
            seq = name_to_seq.get(name)
            if seq:
                await self.ws.subscribe_condition(seq)
                logger.info(f"   '{name}' -> seq={seq}")
            else:
                logger.warning(f"   '{name}' 조건식 없음")

        if not settings.CONDITION_NAMES and settings.CONDITION_NOS:
            for seq in settings.CONDITION_NOS:
                if seq in cond_map:
                    await self.ws.subscribe_condition(seq)

    async def _process_initial_snapshot(self):
        """봇 시작 시 조건식에 이미 들어있는 종목들을 가져와서 처리.
        키움 실시간 구독은 등록 이후 신규 편입만 알려주므로 이게 없으면 첫 윈도우를 놓침.
        조건별 출처(급등 여부)를 종목에 태깅해서 strategy로 전달."""
       

        cond_map = self.ws.condition_map
        name_to_seq = {name: seq for seq, name in cond_map.items()}

        code_is_surge: dict[str, bool] = {}
        for name in settings.CONDITION_NAMES:
            seq = name_to_seq.get(name)
            if not seq:
                continue
            is_surge = seq in self.surge_seqs
            try:
                codes = await self.ws.fetch_condition_snapshot(seq)
            except Exception:
                logger.exception(f"조건식 스냅샷 실패: {name}")
                continue
            self._known_hits[seq] = set(codes)  # 폴링이 중복 잡지 않도록 미리 등록
            for c in codes:
                # 같은 종목이 여러 조건에 들면 급등(True)을 우선
                code_is_surge[c] = code_is_surge.get(c, False) or is_surge

        if not code_is_surge:
            logger.info("📸 초기 스냅샷: 진입 종목 없음")
            return

        logger.info(f"📸 초기 스냅샷 처리 시작: {len(code_is_surge)}종목")
        for code, is_surge in code_is_surge.items():
            self._signal_stats["snapshot"] += 1
            stock_name = self._fetch_stock_name(code)

            try:
                self.strategy_mgr.on_condition_hit(code, stock_name, is_surge=is_surge)
            except Exception:
                logger.exception(f"[{code}] 스냅샷 on_condition_hit 예외")

            if code not in self._subscribed:
                try:
                    await self.ws.subscribe_realtime([code], ["0B", "0D"])
                    self._subscribed.add(code)
                except Exception:
                    logger.exception(f"[{code}] 스냅샷 실시간 구독 실패")

            await asyncio.sleep(SNAPSHOT_STAGGER_SEC)

        logger.info(f"📸 초기 스냅샷 처리 완료: {len(code_is_surge)}종목")
    SUB_BATCH_SIZE = 3      # 한 REG에 묶을 종목 수
    SUB_FLUSH_SEC = 2.0     # 버퍼 미달 시 플러시 대기

    async def _enqueue_subscribe(self, stock_code: str):
        """0B/0D 구독을 버퍼에 추가. 3개 차면 즉시 배치 발사."""
        async with self._sub_buffer_lock:
            if stock_code in self._subscribed or stock_code in self._sub_buffer:
                return
            self._sub_buffer.append(stock_code)
            self._last_buffer_add = time.time()
            if len(self._sub_buffer) >= self.SUB_BATCH_SIZE:
                batch = self._sub_buffer[:self.SUB_BATCH_SIZE]
                self._sub_buffer = self._sub_buffer[self.SUB_BATCH_SIZE:]
                await self._flush_subscribe(batch)

    async def _flush_subscribe(self, batch: list[str]):
        """종목 묶음을 한 REG로 구독."""
        if not batch:
            return
        try:
            await self.ws.subscribe_realtime(batch, ["0B", "0D"])
            self._subscribed.update(batch)
        except Exception:
            logger.exception(f"배치 구독 실패: {batch}")

    async def task_subscribe_flush(self):
        """버퍼에 남은(3개 미만) 종목을 SUB_FLUSH_SEC 후 발사."""
        while not self._stop:
            await asyncio.sleep(1)
            async with self._sub_buffer_lock:
                if (self._sub_buffer
                        and time.time() - self._last_buffer_add >= self.SUB_FLUSH_SEC):
                    batch = self._sub_buffer
                    self._sub_buffer = []
                    await self._flush_subscribe(batch)

    async def task_condition_snapshot_poll(self):
        """주기적으로 조건식 결과 재조회 -> 새 종목만 on_condition_hit.
        실시간 push 보완 + 9시 전 강건성. setup 막지 않는 백그라운드 태스크."""
        while not self._stop:
            await asyncio.sleep(POLL_INTERVAL_SEC)
            cond_map = self.ws.condition_map
            name_to_seq = {name: seq for seq, name in cond_map.items()}
            for name in settings.CONDITION_NAMES:
                seq = name_to_seq.get(name)
                if not seq:
                    continue
                try:
                    codes = await self.ws.fetch_condition_snapshot(seq)
                except Exception:
                    logger.exception(f"폴링 스냅샷 실패: {name}")
                    continue
                current = set(codes)
                known = self._known_hits.setdefault(seq, set())
                new_codes = current - known
                self._known_hits[seq] = current  # 이탈 자동 반영(재편입 가능)
                if not new_codes:
                    await asyncio.sleep(0.3)
                    continue
                is_surge = seq in self.surge_seqs
                logger.info(f"🎣 폴링 신규 {name}: {len(new_codes)}종목 (surge={is_surge})")
                for c in new_codes:
                    self._signal_stats["poll"] += 1
                    stock_name = self._fetch_stock_name(c)
                    try:
                        self.strategy_mgr.on_condition_hit(c, stock_name, is_surge=is_surge)
                    except Exception:
                        logger.exception(f"[{c}] on_condition_hit 예외")
                    await self._enqueue_subscribe(c)
                await asyncio.sleep(0.3)  # 조건별 throttle
    def _fetch_stock_name(self, stock_code: str) -> str:
        """REST로 종목명 조회 (캐시 활용)."""
        try:
            return self.order_mgr.get_stock_name(stock_code)
        except Exception:
            return stock_code

    async def _on_signal(self, stock_code: str, signal_type: str,
                         raw: dict = None, cond_seq: str = None):
        if signal_type == 'I':
            self._signal_stats["insert"] += 1
            self._signal_stats["buy_attempted"] += 1

            is_surge = bool(cond_seq) and str(cond_seq) in self.surge_seqs

            stock_name = _extract_stock_name(raw, stock_code)
            # raw에 종목명이 없으면 REST로 조회 (모의투자 fallback)
            if stock_name == stock_code:
                stock_name = self._fetch_stock_name(stock_code)
                if not self._raw_keys_logged and raw:
                    logger.info(
                        f"[{stock_code}] raw에 종목명 없음 (키: {list(raw.keys())}), "
                        f"REST 조회 → '{stock_name}'"
                    )
                    self._raw_keys_logged = True
            
            if cond_seq:
                self._known_hits.setdefault(str(cond_seq), set()).add(stock_code)
            
            try:
                self.strategy_mgr.on_condition_hit(
                    stock_code, stock_name, is_surge=is_surge
                )
            except Exception:
                logger.exception(f"[{stock_code}] on_condition_hit 예외")
            await self._enqueue_subscribe(stock_code)

        elif signal_type == 'D':
            self._signal_stats["delete"] += 1

    async def _on_trade(self, parsed_trade: dict):
        try:
            self.strategy_mgr.on_trade(parsed_trade)
        except Exception:
            logger.exception("on_trade 예외")

    async def _on_orderbook(self, parsed_orderbook: dict):
        try:
            self.strategy_mgr.on_orderbook(parsed_orderbook)
        except Exception:
            logger.exception("on_orderbook 예외")

    async def task_strategy_tick(self):
        while not self._stop:
            await asyncio.sleep(STRATEGY_TICK_INTERVAL)
            try:
                self.strategy_mgr.tick()
            except Exception:
                logger.exception("Strategy tick 예외")

    async def task_holdings_price_fallback(self):
        while not self._stop:
            await asyncio.sleep(POSITION_CHECK_INTERVAL)
            try:
                for code in list(self.strategy_mgr.holdings.keys()):
                    candles = self.rest.get_minute_candles(code, interval=1, count=1)
                    if candles:
                        self.strategy_mgr.on_price_update(code, candles[0]["close"])
            except Exception:
                logger.exception("보유 종목 가격 폴링 예외")

    async def task_balance_sync(self):
        while not self._stop:
            await asyncio.sleep(SYNC_INTERVAL)
            try:
                self.order_mgr.sync_positions_from_server()
            except Exception:
                logger.exception("잔고 동기화 예외")

    async def task_status_report(self):
        while not self._stop:
            await asyncio.sleep(STATUS_REPORT_INTERVAL)
            try:
                deposit = self.rest.get_orderable_amount()
                h = self.strategy_mgr.holdings
                lines = [
                    f"[정기보고] {datetime.now().strftime('%H:%M')}",
                    f"주문가능: {deposit:,}원",
                    f"신호: 편입 {self._signal_stats['insert']}건 / "
                    f"이탈 {self._signal_stats['delete']}건 / "
                    f"스냅샷 {self._signal_stats['snapshot']}건 / "
                    f"폴링 {self._signal_stats['poll']}건",
                    f"매수시도: {self._signal_stats['buy_attempted']}건",
                    f"보유: {len(h)}종목 "
                    f"(1A=..., 1B=..., 2=..., 3={self.strategy_mgr.count_holdings_by_strategy('3')})",
                    f"감시 중 (1B FSM): {len(self.phase1b_ctrl.watched)}종목, "
                    f"(3 FSM): {len(self.phase3_ctrl.watched)}종목",
                ]
                send_telegram("\n".join(lines), target="signal")
            except Exception:
                logger.exception("상태 보고 예외")

    async def task_token_refresh(self):
        while not self._stop:
            await asyncio.sleep(TOKEN_REFRESH_INTERVAL)
            try:
                new_token = get_access_token()
                if new_token:
                    self.token = new_token
                    self.rest.token = new_token
                    logger.info("토큰 갱신 완료")
            except Exception:
                logger.exception("토큰 갱신 예외")

    async def task_force_close_watcher(self):
        initial_skip = datetime.now().strftime("%H:%M") >= FORCE_CLOSE_TIME
        if initial_skip:
            logger.info(
                "봇 시작 시점이 장마감 이후 — 오늘 강제청산 건너뜀, "
                "다음 거래일 대기"
            )
        triggered = initial_skip
        last_check_date = datetime.now().date()

        while not self._stop:
            now = datetime.now()
            today = now.date()
            if today > last_check_date:
                triggered = False
                last_check_date = today

            now_str = now.strftime("%H:%M")
            if now_str >= FORCE_CLOSE_TIME and not triggered:
                triggered = True
                logger.info("장마감 강제청산 시작")
                for code in list(self.strategy_mgr.holdings.keys()):
                    try:
                        candles = self.rest.get_minute_candles(code, interval=1, count=1)
                        if candles:
                            self.strategy_mgr._execute_sell(
                                code, candles[0]["close"], "장마감 강제청산"
                            )
                    except Exception:
                        logger.exception(f"[{code}] 강제청산 실패")
                await asyncio.sleep(60)
            await asyncio.sleep(10)

    async def task_signal_watchdog(self):
        last_signal_count = 0
        last_signal_time = time.time()

        while not self._stop:
            await asyncio.sleep(SIGNAL_WATCHDOG_INTERVAL)

            if len(self.strategy_mgr.holdings) >= MAX_POSITIONS:
                last_signal_time = time.time()
                continue

            current_count = self._signal_stats["insert"]
            if current_count > last_signal_count:
                last_signal_count = current_count
                last_signal_time = time.time()
                continue

            elapsed = time.time() - last_signal_time
            if elapsed > SIGNAL_TIMEOUT:
                minutes = int(elapsed / 60)
                logger.warning(f"{minutes}분간 신호 없음 -> 조건식 재등록")
                try:
                    await self._subscribe_conditions()
                    last_signal_time = time.time()
                    send_telegram(
                        f"조건식 자동 재등록 ({minutes}분 무신호 감지)",
                        target="signal"
                    )
                except Exception:
                    logger.exception("조건식 재등록 실패")

    async def run(self):
        await self.setup()
        try:
            await asyncio.gather(
                self.ws.listen(),
                self.task_strategy_tick(),
                self.task_holdings_price_fallback(),
                self.task_balance_sync(),
                self.task_status_report(),
                self.task_token_refresh(),
                self.task_force_close_watcher(),
                self.task_signal_watchdog(),
                self.task_subscribe_flush(),
                #self.task_condition_snapshot_poll(),
            )
        except asyncio.CancelledError:
            pass

    async def shutdown(self):
        logger.info("봇 종료 절차 시작")
        self._stop = True

        logger.info(f"최종 신호 통계: {self._signal_stats}")
        if self.strategy_mgr:
            h = self.strategy_mgr.holdings
            logger.info(
                f"보유: {len(h)}종목 "
                f"(1A={self.strategy_mgr.count_holdings_by_strategy('1A')}, "
                f"1B={self.strategy_mgr.count_holdings_by_strategy('1B')}, "
                f"2={self.strategy_mgr.count_holdings_by_strategy('2')}, "
                f"3={self.strategy_mgr.count_holdings_by_strategy('3')})"
            )

        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

        msg = (f"봇 종료 ({datetime.now().strftime('%H:%M:%S')})\n"
               f"보유: {len(self.strategy_mgr.holdings) if self.strategy_mgr else 0}종목")
        send_telegram(msg, target="signal")
        logger.info("안녕히")


async def main():
    bot = TradingBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("봇 실행 중 치명적 예외")
        send_telegram(f"봇 비정상 종료", target="signal")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n종료")
       