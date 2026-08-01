"""
자동매매 봇 진입점
실행: python main.py
종료: Ctrl+C
"""
import asyncio
import time
import os
from datetime import datetime, timedelta

from api.auth import get_access_token, send_telegram
from api.kiwoom_rest import KiwoomREST
from api.kiwoom_ws import KiwoomWS
from core.order_manager import OrderManager, FORCE_CLOSE_TIME, MAX_POSITIONS
from core.condition_manager import ConditionManager
from core.phase1b_controller import Phase1BController
from core.strategy_manager import StrategyManager
from core.strategy.portfolio_optimizer import PortfolioOptimizer
from config import settings
from utils.logger import logger
from db import TradeRepository


POSITION_CHECK_INTERVAL = 30
SYNC_INTERVAL = 15
STATUS_REPORT_INTERVAL = 1800
# 매수 직후 서버 잔고 반영 지연으로 인한 오탐(수동매도로 착각) 방지 유예시간.
# SYNC_INTERVAL(15초)보다 넉넉히 커야 최소 1회 이상의 정상 동기화 주기를 보장함. (2026-07-28)
RECONCILE_GRACE_SECONDS = 45
TOKEN_REFRESH_INTERVAL = 23 * 3600
SIGNAL_WATCHDOG_INTERVAL = 300
SIGNAL_TIMEOUT = 1800
STRATEGY_TICK_INTERVAL = 10
SNAPSHOT_STAGGER_SEC = 0.5  # 스냅샷 종목 처리 간격
POLL_INTERVAL_SEC = 20      # 조건검색 주기 폴링 간격(초)


def time_in(now: datetime, h: int, m: int) -> bool:
    """now가 h:m 이후인지 (같은 날 기준)."""
    return (now.hour, now.minute) >= (h, m)


def time_after(now: datetime, h: int, m: int) -> bool:
    """now가 h:m을 지났는지."""
    return (now.hour, now.minute) > (h, m)


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
        self._last_signal_time = time.time()  # task_signal_watchdog와 WS 재연결 핸들러가 공유

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
        self.strategy_mgr = StrategyManager(
            kiwoom_rest=self.rest,
            order_manager=self.order_mgr,
            phase1b_controller=self.phase1b_ctrl,
            portfolio_optimizer=self.optimizer,
        )

        self.condition_manager = ConditionManager(self.order_mgr)
        self.ws = KiwoomWS(
            self.token,
            condition_manager=self.condition_manager,
            is_mock=settings.IS_MOCK,
            on_signal=self._on_signal,
            on_trade=self._on_trade,
            on_orderbook=self._on_orderbook,
            on_program=self._on_program,
            on_disconnect=self._on_ws_disconnect,
            on_reconnect=self._on_ws_reconnect,
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
               f"눌림={self.strategy_mgr.count_holdings_by_strategy('1A_눌림')}, "
               f"1B={self.strategy_mgr.count_holdings_by_strategy('1B')}, "
               f"1L={self.strategy_mgr.count_holdings_by_strategy('1L')})\n"
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
        # 종목 하나가 여러 조건식에 동시에 걸릴 수 있어서, 어느 조건(들)에서
        # 왔는지 그대로 보존(2026-07-29) — 기존엔 전부 "초기스냅샷"이라는
        # 뭉뚱그린 라벨로 덮어써서 실제 조건검색식 이름(주도주상위/체결강도100/
        # 돌파자동매매용/관심종목감시)과 중복편입 여부를 알 수 없었음.
        code_conditions: dict[str, list[str]] = {}
        for name in settings.CONDITION_NAMES:
            seq = name_to_seq.get(name)
            if not seq:
                continue
            is_surge = seq in self.surge_seqs
            try:
                codes = await self.ws.fetch_condition_snapshot(seq)

                # 👇 [추가된 부분] seq=3 데이터가 None으로 올 때 튕김 방지
                if codes is None:
                    codes = []

            except Exception:
                logger.exception(f"조건식 스냅샷 실패: {name}")
                continue
            self._known_hits[seq] = set(codes)  # 폴링이 중복 잡지 않도록 미리 등록
            for c in codes:
                code_is_surge[c] = code_is_surge.get(c, False) or is_surge
                code_conditions.setdefault(c, []).append(name)

        if not code_is_surge:
            logger.info("📸 초기 스냅샷: 진입 종목 없음")
            return

        logger.info(f"📸 초기 스냅샷 처리 시작: {len(code_is_surge)}종목")
        for code, is_surge in code_is_surge.items():
            self._signal_stats["snapshot"] += 1
            stock_name = self._fetch_stock_name(code)
            cond_name = "+".join(code_conditions.get(code, [])) or "초기스냅샷"

            try:
                await asyncio.to_thread(
                    self.strategy_mgr.on_condition_hit,
                    code, stock_name, is_surge=is_surge, cond_name=cond_name,
                )
            except Exception:
                logger.exception(f"[{code}] 스냅샷 on_condition_hit 예외")

            if code not in self._subscribed:
                try:
                    await self.ws.subscribe_realtime([code], ["0B", "0D", "0g"])
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
            await self.ws.subscribe_realtime(batch, ["0B", "0D", "0g"])
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

                    # 👇 [추가된 부분] seq=3 데이터가 None으로 올 때 튕김 방지
                    if codes is None:
                        codes = []

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
                        await asyncio.to_thread(
                            self.strategy_mgr.on_condition_hit,
                            c, stock_name, is_surge=is_surge, cond_name=name,
                        )
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

    async def _on_signal(
        self, stock_code: str, signal_type: str, raw: dict = None, cond_seq: str = None
    ):
        if signal_type == "I":
            self._signal_stats["insert"] += 1
            self._signal_stats["buy_attempted"] += 1

            is_surge = bool(cond_seq) and str(cond_seq) in self.surge_seqs

            stock_name = _extract_stock_name(raw, stock_code)
            if stock_name == stock_code:
                stock_name = self._fetch_stock_name(stock_code)
                if not self._raw_keys_logged and raw:
                    logger.info(
                        f"[{stock_code}] raw에 종목명 없음 (키: {list(raw.keys())}), "
                        f"REST 조회 -> '{stock_name}'"
                    )
                self._raw_keys_logged = True

            if cond_seq:
                self._known_hits.setdefault(str(cond_seq), set()).add(stock_code)

            # [추가] cond_seq를 이름으로 변환
            cond_name = self.ws.condition_map.get(str(cond_seq), "")
            if not cond_name:
                # 키움 실시간 편입 payload엔 조건 seq가 없는 경우가 많다(실측:
                # raw 키가 ['jmcode'] 하나뿐, 2026-07-23부터 여러 날 동일).
                # 이때 "기타"로 뭉개면 조건별 시간 게이트(OTHER_COND_START)가
                # 주도주상위 종목까지 09:20까지 잘못 지연시킨다 — 07-30 실전에서
                # 09:01~09:20 매매가 통째로 멈춘 원인. 스냅샷/폴링으로 이미
                # 축적된 _known_hits(seq -> 종목 set)에서 역으로 이 종목이 속한
                # 조건식 이름을 복원한다. (2026-07-30)
                matched = [
                    name
                    for seq, codes in self._known_hits.items()
                    if stock_code in codes
                    for name in [self.ws.condition_map.get(str(seq), "")]
                    if name
                ]
                cond_name = "+".join(matched) if matched else "기타"

            try:
                # [수정] cond_name을 추가로 전달
                await asyncio.to_thread(
                    self.strategy_mgr.on_condition_hit,
                    stock_code, stock_name, is_surge=is_surge, cond_name=cond_name,
                )
            except Exception:
                logger.exception(f"[{stock_code}] on_condition_hit 예외")

            await self._enqueue_subscribe(stock_code)

        elif signal_type == "D":
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

    async def _on_program(self, parsed_program: dict):
        """종목프로그램매매(0g) 콜백 — 매매 판단에는 미연결, 기록 전용 (2026-07-31).
        REST 호출 없이 WS로 공짜로 들어오는 데이터라 여기서 바로 누적값을
        program_flow에 넘긴다(델타 변환은 ProgramFlowTracker.record_cumulative가 처리)."""
        try:
            strat = self.strategy_mgr
            if not strat or not getattr(strat, "program_flow", None):
                return
            code = parsed_program.get("stock_code")
            net_amt = parsed_program.get("net_amt_cum")
            if not code or net_amt is None:
                return
            name = strat._stock_names.get(code, code)
            strat.program_flow.record_cumulative(code, net_amt, stock_name=name)
        except Exception:
            logger.exception("on_program 예외")

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
                    candles = await asyncio.to_thread(
                        self.rest.get_minute_candles, code, interval=1, count=1
                    )
                    if candles:
                        self.strategy_mgr.on_price_update(code, candles[0]["close"])
            except Exception:
                logger.exception("보유 종목 가격 폴링 예외")

    async def task_balance_sync(self):
        while not self._stop:
            await asyncio.sleep(SYNC_INTERVAL)
            try:
                server_positions = await asyncio.to_thread(
                    self.order_mgr.sync_positions_from_server
                )
                self._reconcile_manual_sells(server_positions)
            except Exception:
                logger.exception("잔고 동기화 실패")

    def _reconcile_manual_sells(self, server_positions: dict):
        """서버 잔고와 strategy_mgr.holdings를 대조해 수동 매도를 반영.
        1) 전량 매도: 서버 잔고에 종목 자체가 없음 -> 슬롯 즉시 해제. (2026-07-26)
        2) 일부 매도: 서버 잔고 수량이 봇이 기억하는 수량보다 적음 -> 보유수량만
           서버 값으로 축소 갱신 (전량 매도로 착각해 슬롯을 빼진 않음). (2026-07-27)
        매수 직후 RECONCILE_GRACE_SECONDS 이내인 종목은 검사 대상에서 제외 —
        모의API 잔고 반영 지연을 실제 매도로 오판해 방금 산 포지션을 놓치는
        사고가 실전에서 확인됨. (2026-07-28)
        """
        strat = self.strategy_mgr
        if not strat:
            return

        now = datetime.now()

        def _in_grace(pos: dict) -> bool:
            buy_time = pos.get("buy_time")
            return bool(
                buy_time
                and (now - buy_time).total_seconds() < RECONCILE_GRACE_SECONDS
            )

        manually_sold = [
            code
            for code in list(strat.holdings.keys())
            if code not in server_positions and not _in_grace(strat.holdings[code])
        ]
        for code in manually_sold:
            info = strat.holdings.pop(code, None)
            strat.pending.discard(code)
            name = (info or {}).get("stock_name", code)
            logger.info("[%s] %s 수동 매도 감지 -> 슬롯 즉시 해제", code, name)

            # DB status를 'closed'로 갱신 안 하면 재시작 시 _restore_from_db()가
            # 이미 팔린 종목을 다시 보유중으로 불러오는 사고로 이어짐 (2026-07-28 실전 확인).
            # 실제 매도가는 알 수 없어 매수가로 대체(수익률 0%) — status 정합성이 우선.
            trade_id = (info or {}).get("trade_id")
            if trade_id:
                try:
                    buy_price = (info or {}).get("buy_price", 0)
                    buy_qty = (info or {}).get("buy_quantity", 0)
                    TradeRepository.update_sell(
                        trade_id,
                        sell_price=buy_price,
                        sell_quantity=buy_qty,
                        exit_reason="수동 매도 감지 (실제 체결가 미상, 매수가로 대체 기록)",
                    )
                except Exception as e:
                    logger.warning("[%s] 수동 매도 DB 정리 실패: %s", code, e)

            if send_telegram:
                send_telegram(
                    f"수동 매도 감지\n{name} ({code})\n슬롯 해제 완료, 다음 시퀀스 진행",
                    target="order",
                )

        for code, pos in list(strat.holdings.items()):
            if _in_grace(pos):
                continue
            server_info = server_positions.get(code)
            if not server_info:
                continue
            server_qty = server_info.get("qty", 0)
            tracked_qty = pos.get("qty", pos.get("buy_quantity", 0))
            if 0 < server_qty < tracked_qty:
                pos["qty"] = server_qty
                name = pos.get("stock_name", code)
                logger.info(
                    "[%s] %s 일부 수동 매도 감지 -> 보유수량 %d주 -> %d주로 갱신",
                    code, name, tracked_qty, server_qty,
                )
                if send_telegram:
                    send_telegram(
                        f"일부 수동 매도 감지\n{name} ({code})\n"
                        f"보유수량 {tracked_qty}주 -> {server_qty}주로 갱신 (남은 수량 자동청산 계속)",
                        target="order",
                    )

    def _on_ws_disconnect(self):
        """WS 단절 감지 직후 1회 호출 (KiwoomWS 콜백, 동기)."""
        logger.warning("🔌 WS 연결 끊김 감지 — 재연결 대기 중")
        send_telegram("⚠️ WS 연결 끊김 감지 — 재연결 시도 중", target="signal")

    async def _on_ws_reconnect(self, outage_seconds: float):
        """WS 재연결(+조건/실시간 재구독) 완료 직후 1회 호출.
        단절 시간대별로 대응강도를 차등 적용:
          - 짧은 순단(<30s): 격리 10초
          - 중간 단절(30s~5분): 격리 60초
          - 긴 단절(5분+): 격리 10분 + 조건검색 스냅샷 재확인
        매수만 보류하고(StrategyManager.can_buy_more) 청산 감시(on_price_update 등)는
        그대로 유지된다 — can_buy_more()는 진입 판정에서만 쓰이기 때문."""
        if outage_seconds < 30:
            tier, quarantine_sec = "짧은 순단", 10
        elif outage_seconds < 300:
            tier, quarantine_sec = "중간 단절", 60
        else:
            tier, quarantine_sec = "긴 단절", 600

        logger.info(f"✅ WS 재연결 완료 ({tier}, 단절 {outage_seconds:.0f}초)")

        resolved_during_outage = []
        still_unfilled = set()
        try:
            # 1) 포지션 즉시 재동기화 (수동매도/단절중 체결분 반영, 15초 대기 안 하고 즉시)
            server_positions = self.order_mgr.sync_positions_from_server()
            self._reconcile_manual_sells(server_positions)

            # 2) 미체결 주문 상태 확인 — 무조건 취소하지 않고, 결론난 것만 pending 해제
            unfilled = self.rest.get_unfilled_orders()
            for item in unfilled.get("oso") or []:
                code = (item.get("stk_cd") or item.get("stock_code") or "").strip().lstrip("A")
                if code:
                    still_unfilled.add(code)

            for code in list(self.strategy_mgr.pending):
                if code not in still_unfilled:
                    resolved_during_outage.append(code)
            self.strategy_mgr.pending -= set(resolved_during_outage)

            if still_unfilled:
                logger.warning(
                    "⚠️ 재연결 후에도 미체결 남은 종목(자동취소 안 함, 수동확인 권장): %s",
                    still_unfilled,
                )
        except Exception:
            logger.exception("재연결 후 주문/포지션 재확인 실패")

        # 3) 격리기간 설정 (신규매수 보류, 청산감시는 계속)
        self.strategy_mgr.quarantine_until = datetime.now() + timedelta(seconds=quarantine_sec)

        # 4) 긴 단절이면 조건검색 스냅샷 재확인 + signal watchdog 타이머 리셋
        if outage_seconds >= 300:
            try:
                await self._process_initial_snapshot()
            except Exception:
                logger.exception("재연결 후 스냅샷 재확인 실패")
        self._last_signal_time = time.time()

        msg = (
            f"🔌 WS 재연결 완료 ({tier}, {outage_seconds:.0f}초)\n"
            f"신규매수 {quarantine_sec}초 보류 (청산감시는 계속 작동)\n"
        )
        if resolved_during_outage:
            msg += f"단절 중 결론난 주문 정리: {resolved_during_outage}\n"
        if still_unfilled:
            msg += f"⚠️ 아직 미체결(자동취소 안 함): {sorted(still_unfilled)}\n"
        send_telegram(msg, target="signal")

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
                    f"(1A={self.strategy_mgr.count_holdings_by_strategy('1A')}, "
                    f"눌림={self.strategy_mgr.count_holdings_by_strategy('1A_눌림')}, "
                    f"1B={self.strategy_mgr.count_holdings_by_strategy('1B')}, "
                    f"1L={self.strategy_mgr.count_holdings_by_strategy('1L')})",
                    f"감시 중 (1B FSM): {len(self.phase1b_ctrl.watched)}종목",
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

    async def task_auto_shutdown(self):
        # 2026-07-27엔 daily_backtest(15:30 트리거)와 순서가 겹쳐서(15:20에 먼저
        # 종료되면 백테스트가 못 돔) 통째로 비활성화했었는데, 그 뒤로 재활성화를
        # 안 해서 2026-07-30 실전에서 장마감(15:15 강제청산) 이후에도 정기보고/
        # WS재연결/조건재등록이 19:32까지 4시간 넘게 계속 돌아간 문제 발생.
        # daily_backtest가 15:30에 트리거돼 보통 30~60초 안에 끝나는 걸 감안해
        # 15:40으로 늦춰서 재활성화(2026-07-30) — 강제청산(15:15)과 백테스트
        # 리포트(15:30) 둘 다 끝난 뒤에만 종료되도록.
        target_time = "15:40"
        while not self._stop:
            now_str = datetime.now().strftime("%H:%M")
            if now_str >= target_time:
                msg = f"⏰ 설정된 시간({target_time}) 도달. 매매를 중지하고 프로그램을 안전하게 자동 종료합니다."
                logger.info(msg)
                send_telegram(msg, target="signal")

                self._stop = True

                # 현재 실행 중인 다른 모든 백그라운드 태스크들을 안전하게 취소하여
                # finally 블록의 bot.shutdown()이 자연스럽게 호출되도록 유도
                tasks = [
                    t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                ]
                for task in tasks:
                    task.cancel()
                break

            await asyncio.sleep(30)  # 30초 후 확실히 종료

    async def task_closing_bet_scanner(self):
        """매일 14:50에 1회, 조건검색 통과 종목 전체를 대상으로 종가베팅
        후보 top 10을 스캔해서 텔레그램으로 전송. 2026-07-26 신규.
        주의: 아래 import는 반드시 try 블록 안에서만 — 코루틴 시작 시점(=봇 기동 시점)에
        바로 실행되는 최상단 import가 실패하면 asyncio.gather() 전체가 죽어서
        봇이 통째로 종료된다 (2026-07-27 실제 장애 원인, evaluate_closing_bet_candidate
        미구현으로 매 기동 시 즉시 크래시)."""
        done_date = None
        while not self._stop:
            await asyncio.sleep(20)  # 20초 주기로 시각 체크 (기존 sleep(5) 관례와 유사)
            now = datetime.now()
            trigger_h, trigger_m = 14, 50
            if now.hour != trigger_h or now.minute < trigger_m:
                continue
            if done_date == now.date():
                continue  # 오늘은 이미 실행함

            done_date = now.date()
            logger.info("🔔 [종가베팅] 스캔 시작...")

            try:
                from core.explosion_scorer import evaluate_closing_bet_candidate
                from core.history_fetcher import to_trade_value_bins
                from core.history_cache import fetch_with_cache, slice_today, cache_stats

                strat = self.strategy_mgr
                candidates = {}
                # 오늘 조건검색에 한 번이라도 걸렸던 종목 전체 대상 (2026-07-28:
                # 거래대금 폭발 이력 준비를 on_condition_hit에서 여기로 이관 —
                # 하루종일 매번 계산하지 않고 실제 필요한 이 시점에 1회만 계산)
                target_codes = list(strat._cond_names.keys())

                def _evaluate_one(code: str):
                    # 영속 캐시 경유 (2026-07-31) — 어제까지의 분봉은 변하지 않으므로
                    # 매일 20일치를 다시 받지 않고 최근 구간만 1콜씩 이어붙인다.
                    # 종목당 5콜 -> 2콜. 자세한 근거는 core/history_cache.py 참고.
                    candles_3m = fetch_with_cache(strat.api, code, interval=3, target_days=20)
                    candles_60m = fetch_with_cache(strat.api, code, interval=60, target_days=20)
                    bins_3m = to_trade_value_bins(candles_3m)
                    bins_60m = to_trade_value_bins(candles_60m)
                    cache_entry = strat.explosion_scorer.prepare(code, bins_3m, bins_60m)

                    # 당일 3분봉은 위 20일치에 이미 들어있다 — 기존의 별도 호출
                    # (_raw_get_minute_candles count=150)은 순수 중복이었고, 게다가
                    # 그 결과가 내림차순(최신->과거)이라 evaluate_closing_bet_candidate의
                    # today_bins[-5:]가 '최근 5봉'이 아니라 '가장 오래된 5봉'(전일
                    # 오후)을 채점하고 있었다. 오름차순 캐시에서 당일만 잘라 쓰면
                    # 호출도 없애고 그 버그도 같이 해결된다. (2026-07-31)
                    today_bins = to_trade_value_bins(slice_today(candles_3m))
                    if not today_bins:
                        return {"eligible": False, "reason": "당일 3분봉 없음"}
                    return evaluate_closing_bet_candidate(
                        today_bins,
                        cache_entry["baseline"],
                        cache_entry["bins_60m_hist"],
                        strat.explosion_scorer.config,
                    )

                for code in target_codes:
                    try:
                        result = await asyncio.to_thread(_evaluate_one, code)
                        if result.get("eligible"):
                            candidates[code] = result
                    except Exception as e:
                        logger.warning("[%s] 종가베팅 평가 실패: %s", code, e)

                st = cache_stats()
                logger.info(
                    "🔔 [종가베팅] 평가 완료 %d종목 (히스토리 캐시 %d파일 %.1fMB)",
                    len(target_codes), st["files"], st["bytes"] / 1024 / 1024,
                )

                ranked = sorted(
                    candidates.items(), key=lambda x: x[1]["closing_score"], reverse=True
                )[:10]

                if not ranked:
                    logger.info("🔔 [종가베팅] 후보 없음")
                    continue

                lines = ["🔔 종가베팅 후보 TOP 10", ""]
                for i, (code, r) in enumerate(ranked, 1):
                    name = strat._stock_names.get(code, code)
                    lines.append(
                        f"{i}. {name}({code}) | 점수 {r['closing_score']:.1f} | "
                        f"surge {r['surge_ratio']:.2f}배 | 양봉비율 {r['bullish_ratio']*100:.0f}%"
                    )
                msg = "\n".join(lines)
                logger.info(msg)
                if send_telegram:
                    send_telegram(msg, target="closing_bet")

            except Exception as e:
                logger.exception("종가베팅 스캔 중 에러: %s", e)

    async def task_tick_archive(self):
        """매일 09:20에 1회, 오늘 조건검색에 걸린 종목들의 개장초반(09:00~09:15)
        틱을 아카이빙 — 1A(체결강도 단독) 백테스트용 데이터 축적 (2026-07-31 신규).
        반드시 09:20 직후에 실행해야 한다 — ka10079가 '현재 시각'에서 과거로
        페이징하는 구조라, 이 태스크가 늦게 돌수록(예: 장마감 후) 09:00~09:15
        구간까지 내려가는 데 필요한 REST 호출이 급격히 늘어난다(실측: 종목당
        최대 185콜까지도 나옴, core/tick_archive.py 모듈 docstring 참고).
        07-31(주말 전)은 소급 수집이 무의미하다고 판단해 건너뛰고 다음 거래일
        (08-03 월요일)부터 이 태스크로 자동 축적 시작."""
        done_date = None
        trigger_h, trigger_m = 9, 20
        while not self._stop:
            await asyncio.sleep(20)
            now = datetime.now()
            if now.hour != trigger_h or now.minute < trigger_m:
                continue
            if done_date == now.date():
                continue

            done_date = now.date()
            try:
                from core.daily_backtest import _get_today_universe
                from core.tick_archive import archive_universe

                universe = _get_today_universe()
                codes = [row["stock_code"] for row in universe]
                if codes:
                    logger.info("[틱아카이브] %d종목 개장초반 틱 수집 시작", len(codes))
                    results = await asyncio.to_thread(
                        archive_universe, self.rest.host, self.token, codes
                    )
                    ok = sum(1 for v in results.values() if v > 0)
                    logger.info("[틱아카이브] 완료: %d/%d종목 성공", ok, len(codes))
                else:
                    logger.info("[틱아카이브] 오늘 신호 종목 없음 — 스킵")
            except Exception:
                logger.exception("틱 아카이브 실행 중 에러")

    async def task_daily_backtest(self):
        """매일 15:30에 1회, 오늘 신호 종목(watch_list_log+trades) 대상으로
        라이브 진입/청산 로직을 분봉 기준 재현해 텔레그램(오토트레이더)으로 전송."""
        done_date = None
        trigger_h, trigger_m = 15, 30
        while not self._stop:
            await asyncio.sleep(20)
            now = datetime.now()
            if now.hour != trigger_h or now.minute < trigger_m:
                continue
            if done_date == now.date():
                continue

            done_date = now.date()
            try:
                from core.daily_backtest import run_daily_backtest

                # 종목별 REST 순차 조회로 시간이 걸리므로 별도 스레드에서 실행
                # (동기 호출을 그대로 두면 이벤트루프가 막힘)
                await asyncio.to_thread(run_daily_backtest, self.rest)
            except Exception:
                logger.exception("일일 백테스트 실행 중 에러")

    async def task_slot_replacement(self):
        """1분마다 정체 종목을 감시종목 고득점 후보로 교체 시도. 2026-07-26 신규."""
        replacement_count = 0
        current_date = None
        while not self._stop:
            await asyncio.sleep(60)
            try:
                from core.slot_replacement import try_slot_replacement

                strat = self.strategy_mgr
                if not strat:
                    continue
                now = datetime.now()
                if current_date != now.date():
                    current_date = now.date()
                    replacement_count = 0
                replacement_count = try_slot_replacement(
                    strat, send_telegram, replacement_count, now
                )
            except Exception:
                logger.exception("슬롯 교체 스캔 중 에러")

    async def task_watchlist_reentry(self):
        """슬롯이 비어있을 때 1A/Pullback watch_list 후보를 재평가해 즉시 매수 시도.
        2026-07-28 신규: on_condition_hit은 종목당 최초 편입 시점 1회만 평가해서,
        그때 슬롯이 꽉 차 있으면 이후 슬롯이 비어도 재시도가 안 되던 문제 수정
        (1B/1L은 실시간 틱 콜백이라 원래도 계속 재시도됨, 이 태스크는 1A/Pullback 전용)."""
        while not self._stop:
            await asyncio.sleep(15)
            try:
                from core.watchlist_reentry import try_watchlist_reentry

                strat = self.strategy_mgr
                if not strat:
                    continue
                await asyncio.to_thread(try_watchlist_reentry, strat, datetime.now())
            except Exception:
                logger.exception("watchlist 재진입 스캔 중 에러")

    async def task_stop_signal_watcher(self):

        """STOP_SIGNAL 파일이 감지되면 안전하게 봇 종료.
        수동 종료용: New-Item "STOP_SIGNAL" -ItemType File 로 트리거."""
        signal_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "STOP_SIGNAL"
        )
        while not self._stop:
            await asyncio.sleep(5)
            if os.path.exists(signal_path):
                msg = "🛑 종료 신호 파일 감지 - 매매를 중지하고 프로그램을 안전하게 종료합니다."
                logger.info(msg)
                send_telegram(msg, target="signal")
                self._stop = True

                try:
                    os.remove(signal_path)
                except Exception:
                    pass

                # 현재 실행 중인 다른 모든 백그라운드 태스크들을 안전하게 취소하여
                # finally 블록의 bot.shutdown()이 자연스럽게 호출되도록 유도
                tasks = [
                    t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                ]
                for task in tasks:
                    task.cancel()
                break

            await asyncio.sleep(30)  # 30초 후 확실히 종료

    async def task_signal_watchdog(self):
        last_signal_count = 0
        # self._last_signal_time: WS 재연결 핸들러(_on_ws_reconnect)도 리셋하므로
        # 지역변수가 아닌 인스턴스 속성으로 공유 (__init__에서 초기화됨)

        while not self._stop:
            await asyncio.sleep(SIGNAL_WATCHDOG_INTERVAL)

            if len(self.strategy_mgr.holdings) >= MAX_POSITIONS:
                self._last_signal_time = time.time()
                continue

            current_count = self._signal_stats["insert"]
            if current_count > last_signal_count:
                last_signal_count = current_count
                self._last_signal_time = time.time()
                continue

            elapsed = time.time() - self._last_signal_time
            if elapsed > SIGNAL_TIMEOUT:
                minutes = int(elapsed / 60)
                logger.warning(f"{minutes}분간 신호 없음 -> 조건식 재등록")
                try:
                    await self._subscribe_conditions()
                    self._last_signal_time = time.time()
                    send_telegram(
                        f"조건식 자동 재등록 ({minutes}분 무신호 감지)",
                        target="signal"
                    )
                except Exception:
                    logger.exception("조건식 재등록 실패")

    async def task_entry_diagnostics(self):
        """장중 진입 진단 알림 (2026-08-01 신규).

        "조건검색엔 계속 포착되는데 매수가 안 된다"를 로그를 뒤지지 않고
        장중에 바로 판단할 수 있게, 원인별 요약을 텔레그램(주식 따릉이)으로
        보낸다. 핵심은 두 가지를 구분해서 보여주는 것:
          - 진입 필터가 '정상적으로' 거르는 중인가 (사유별 종목 수)
          - 데이터가 안 들어와 판단 자체를 못 하는가 (= 코드/구독 이상 의심)

        조건검색 수신 통계(편입/스냅샷 건수, 마지막 수신 후 경과)는 main만
        알고 있으므로 여기서 헤더로 붙이고, 전략 내부 상태는 StrategyManager가
        만든다. 09:05~14:50 사이에 DIAG_INTERVAL 주기로만 발송.
        """
        # 알림 피로 방지: 평상시엔 30분 주기, 경고(⚠️)가 잡히면 10분 주기.
        # 매 10분 무조건 보내면 하루 35건이라 정작 중요한 경고가 묻힌다.
        DIAG_INTERVAL_QUIET = 1800   # 30분
        DIAG_INTERVAL_ALERT = 600    # 10분
        last_sent = 0.0
        while not self._stop:
            await asyncio.sleep(30)
            try:
                now = datetime.now()
                if not (time_in(now, 9, 5) and not time_after(now, 14, 50)):
                    continue

                strat = self.strategy_mgr
                if not strat:
                    continue

                body = await asyncio.to_thread(strat.build_entry_diagnostics)
                has_warn = "⚠️" in body or "⛔" in body
                interval = DIAG_INTERVAL_ALERT if has_warn else DIAG_INTERVAL_QUIET
                if time.time() - last_sent < interval:
                    continue
                last_sent = time.time()

                idle = int((time.time() - self._last_signal_time) / 60)
                header = (
                    f"조건검색 수신: 편입 {self._signal_stats['insert']}건 / "
                    f"스냅샷 {self._signal_stats['snapshot']}건 / "
                    f"폴링 {self._signal_stats['poll']}건 (마지막 신호 {idle}분 전)"
                )
                if self._signal_stats["insert"] == 0 and time_in(now, 9, 30):
                    header += "\n⚠️ 실시간 편입 수신 0건 — WS 조건검색 구독 확인 필요"

                send_telegram(f"{body}\n{header}", target="signal")
            except Exception:
                logger.exception("진입 진단 알림 중 에러")

    async def task_program_flow(self):
        """프로그램 매매 유입 기록 — 60초마다 완성된 분을 CSV로 flush,
        10분마다 '꾸준히 들어오는 종목' 요약 로그. (2026-07-31 신규)

        중요: 이 태스크는 **REST 호출을 하지 않는다**. 07-30 실측 기준 429가
        하루 2,469건(장중 매 분 발생)이라 종목별 폴링을 추가할 예산이 없기
        때문이다. 데이터는 외부에서 tracker.record_minute()으로 넣어주는 구조
        (core/program_flow.py 모듈 주석의 '호출 예산' 항목 참고).
        소스가 아직 안 붙어 있어도 태스크 자체는 무해하게 돈다 —
        기록이 없으면 flush 0행, 요약은 '추적 0종목'으로만 남는다."""
        last_report = 0.0
        while not self._stop:
            await asyncio.sleep(60)
            try:
                strat = self.strategy_mgr
                if not strat or not getattr(strat, "program_flow", None):
                    continue
                pf = strat.program_flow
                await asyncio.to_thread(pf.flush)
                now_ts = time.time()
                if now_ts - last_report >= 600:
                    last_report = now_ts
                    logger.info(pf.report())
            except Exception:
                logger.exception("프로그램 유입 기록 중 에러")

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
                self.task_auto_shutdown(),  # 15:40으로 재활성화 (2026-07-30, 위 주석 참고)
                self.task_stop_signal_watcher(),
                self.task_closing_bet_scanner(),
                self.task_slot_replacement(),
                self.task_watchlist_reentry(),
                self.task_daily_backtest(),
                self.task_program_flow(),
                self.task_tick_archive(),
                self.task_entry_diagnostics(),
                # self.task_condition_snapshot_poll(),
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
                f"눌림={self.strategy_mgr.count_holdings_by_strategy('1A_눌림')}, "
                f"1B={self.strategy_mgr.count_holdings_by_strategy('1B')}, "
                f"1L={self.strategy_mgr.count_holdings_by_strategy('1L')})"
            )

        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

        msg = (f"봇 종료 ({datetime.now().strftime('%H:%M:%S')})\n"
               f"보유: {len(self.strategy_mgr.holdings) if self.strategy_mgr else 0}종목")
        try:
            send_telegram(msg, target="signal")
        except Exception:
            logger.exception("종료 알림 전송 실패 (종료 절차는 계속 진행)")
        logger.info("안녕히")


async def main():
    bot = TradingBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("봇 실행 중 치명적 예외")
        try:
            send_telegram(f"봇 비정상 종료", target="signal")
        except Exception:
            logger.exception("비정상 종료 알림 전송 실패")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
   
    # ==========================================

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n종료")
