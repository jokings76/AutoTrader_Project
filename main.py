"""
자동매매 봇 진입점
실행: python main.py
종료: Ctrl+C
"""
import asyncio
import time
import os
import subprocess
from datetime import datetime, timedelta

from api.auth import get_access_token, send_telegram
from api.kiwoom_rest import KiwoomREST
from api.kiwoom_ws import KiwoomWS
from core.order_manager import (
    OrderManager, FORCE_CLOSE_TIME, FORCE_CLOSE_ENABLED, MAX_POSITIONS,
)
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
# 🔴 (2026-08-12 장중 결함수정) 45초로는 부족했다.
#   실측: 알트(459550) 09:01:06 매수 -> 09:02:07 "수동 매도 감지"로 슬롯 해제
#         -> **09:02:45에 서버 잔고 246주가 그대로 있음**(반영에 99초).
#   개장 직후엔 키움 잔고 반영이 45초를 넘는다. 그걸 '사용자가 팔았다'로 오판하면
#   ① DB가 손익 0으로 닫혀 통계가 오염되고(08-11에 고치려던 바로 그 문제)
#   ② 슬롯이 풀렸다 다시 잡히며 **같은 종목을 재매수해 왕복 수수료를 반복 지출**한다.
#   실제로 08-12 오전에 10건이 이렇게 처리됐고 동양파일·코칩이 두 번씩 매수됐다.
#
#   -> 판별을 시간만이 아니라 **`seen_on_server`(서버에서 한 번이라도 확인됐는가)**와
#      묶는다. 한 번도 안 보인 종목은 '아직 반영 전'일 수 있으므로 더 기다린다.
#      ⚠️ 무한정 기다리지는 않는다 — 진짜 미체결이면 holdings에 영원히 남아
#      슬롯을 점유하기 때문이다. 상한을 넘으면 종전대로 정리한다.
#      (그 뒤에도 안전망이 있다: 매도 시 800033이면 `_release_ghost_position`.)
RECONCILE_UNSEEN_GRACE_SECONDS = 300
TOKEN_REFRESH_INTERVAL = 23 * 3600
SIGNAL_WATCHDOG_INTERVAL = 300
SIGNAL_TIMEOUT = 1800
STRATEGY_TICK_INTERVAL = 10
SNAPSHOT_STAGGER_SEC = 0.5  # 스냅샷 종목 처리 간격
# 조건검색 주기 폴링 간격(초). (2026-08-07) 20 -> 60으로 완화하고 폴링을
# 재활성화했다. 이건 **실시간 push를 놓쳤을 때의 백업**이지 주 수집 경로가
# 아니다. 20초면 조건식 3개 x 3회/분 = 시간당 540건이 라이브 소켓에 실리는데,
# 놓친 종목은 [F] 진입 숙성(30~60초) 때문에 어차피 그 안에 못 사므로
# 60초로도 실질 손실이 없다.
POLL_INTERVAL_SEC = 60


def time_in(now: datetime, h: int, m: int) -> bool:
    """now가 h:m 이후인지 (같은 날 기준)."""
    return (now.hour, now.minute) >= (h, m)


def time_after(now: datetime, h: int, m: int) -> bool:
    """now가 h:m을 지났는지."""
    return (now.hour, now.minute) > (h, m)


def _norm_seq(seq) -> str:
    """조건식 seq 정규화 — 앞자리 0을 떼고 비교한다 (2026-08-02).

    HTS 화면은 조건식을 [000]/[001]/[005]처럼 0을 채워 표시하는데, API가
    주는 값은 실측상 패딩이 없다(등록 응답 seq=1,2,3 / 실시간 FID 841='1','3').
    둘이 섞이면 '6' != '006'으로 조용히 어긋나고, 그러면 종가베팅 종목이
    매매 라우팅 차단을 통과해 장중 1A 후보로 새어 들어간다 — 에러가 안 나서
    로그만으로는 알아채기 어려운 종류의 사고다. 양쪽을 정규화해서 막는다.
    """
    s = str(seq if seq is not None else "").strip()
    return s.lstrip("0") or ("0" if s else "")


def _extract_stock_name(raw: dict, stock_code: str) -> str:
    """raw 페이로드에서 종목명을 뽑는다. 못 찾으면 stock_code를 그대로 반환해
    호출부가 REST 폴백(_fetch_stock_name)을 타도록 신호한다.

    ⚠️ 후보 키에 **'name'을 넣으면 안 된다** (2026-08-03 실거래로 확인).
    키움 실시간 편입 push(type='02')의 최상위 'name'은 종목명이 아니라
    **실시간 타입 라벨("조건검색")**이다:
        {'type':'02', 'name':'조건검색', 'item':'079650',
         'values':{'841':'3','9001':'079650','843':'I','20':'100621'}}
    즉 이 페이로드에는 종목명이 아예 없다. 그런데 'name'을 후보로 두면
    "조건검색"을 찾아냈다고 판단해버리고, 그 값이 stock_code와 다르므로
    호출부의 `if stock_name == stock_code:` REST 폴백이 **영영 실행되지
    않는다**. 그 결과 매수 알림·로그·holdings의 종목명이 전부 "조건검색"으로
    찍혔다(08-03 텔레그램 '매수 체결' 3건 모두).
    기동 스냅샷(CNSRREQ)은 '302'에 진짜 종목명이 실려 오므로 정상이었고,
    08-01에 실시간 편입 파싱을 되살리면서 이 경로가 처음 살아나 드러났다.
    """
    if not isinstance(raw, dict):
        return stock_code
    for key in ("302", "hng_name", "stock_name", "kor_name", "jongmok"):
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
        self._orphan_notified: set[str] = set()  # 유령 포지션 알림 중복 방지 (2026-08-01)
        # 장마감 마무리 작업 완료 플래그 (2026-08-03) — 셋 다 끝나면 즉시 종료한다.
        # 예전엔 15:40 고정 시각까지 무조건 기다려서, 할 일이 다 끝난 뒤에도
        # 테마 재계산/조건검색 수신이 계속 돌았다.
        self._closing_bet_done = False   # 14:50 종가베팅 스캔·전송
        self._force_close_done = False   # 15:10 강제청산
        self._backtest_done = False      # 15:30 일일 백테스트
        self._last_signal_time = time.time()  # task_signal_watchdog와 WS 재연결 핸들러가 공유

        # ── 종가베팅 전용 조건검색식 (2026-08-02 신규) ───────────────
        # 장중 3개 검색식(주도주상위/눌림목자동/돌파자동매매용)은 '급등·눌림'을
        # 고르는 스크린이라 오버나이트 보유에는 성질이 반대다(실측: 07-28~31
        # 후보 6종목 중 5개가 코스닥 중소형, 그중 폴라리스AI는 장중 유동성
        # 필터로 걸러내던 종목이었다). 그래서 종가베팅은 자체 검색식을 쓴다.
        #
        # ⚠️ 이 seq는 **매매 라우팅에 절대 넣지 않는다**. CONDITION_NAMES에
        # 그냥 추가하면 resolve_strategy가 "둘 다 아님 -> 1A" 폴백으로 처리해
        # 장중 1A 매수 후보가 되어버린다(strategy_manager.resolve_strategy 참고).
        # _on_signal에서 이 seq를 먼저 걸러내고 on_condition_hit을 아예 안 부른다.
        self._closing_bet_seq: str = ""
        self._closing_bet_codes: set[str] = set()

    async def setup(self):
        logger.info("=" * 60)
        logger.info("자동매매 봇 시작")
        logger.info(f"   모드: {'모의투자' if settings.IS_MOCK else '실전'}")
        logger.info(f"   조건식: {settings.CONDITION_NAMES}")
        logger.info("=" * 60)

        # 낡은 STOP_SIGNAL 정리 (2026-08-02 신규).
        # 이 파일은 '지금 돌고 있는 봇을 세우라'는 일회성 트리거인데,
        # 봇이 이미 죽은 뒤에 만들어지면 아무도 소비하지 않고 그대로 남는다.
        # 그 상태로 다음날 08:59 스케줄러가 봇을 띄우면
        # task_stop_signal_watcher가 5초 안에 감지해서 **기동 즉시 종료**시킨다
        # (무인 기동이라 아무도 모른 채 하루 매매가 통째로 사라진다).
        # 실제로 2026-08-02에 13:59 정상종료 후 14:14에 만들어진 고아 파일이
        # 남아 있는 것을 발견했다. 기동 시점의 신호는 정의상 '이전 세션의
        # 잔재'이므로 여기서 지우고 시작한다.
        _stale_stop = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "STOP_SIGNAL"
        )
        if os.path.exists(_stale_stop):
            try:
                os.remove(_stale_stop)
                logger.warning("⚠️ 낡은 STOP_SIGNAL 발견 — 이전 세션 잔재로 보고 삭제 후 기동")
            except Exception:
                logger.exception("낡은 STOP_SIGNAL 삭제 실패 — 기동 직후 종료될 수 있음")

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

        # 종가베팅 유니버스 초기 스냅샷 (2026-08-02).
        # 실시간 편입은 '구독 이후 새로 들어온 것'만 알려주므로, 장중에
        # 재시작하면 그 전까지 쌓인 멤버십을 통째로 잃는다. 여기서 한 번
        # 받아두면 재시작해도 14:50 유니버스가 온전하다.
        # ⚠️ listen() 시작 전(setup 단계)이라 _wait_for가 소켓을 독점해도
        #    안전하다. 장중 임의 시점에 부르면 체결틱을 흘리므로 금지.
        await self._snapshot_closing_bet_universe()

        deposit = self.rest.get_orderable_amount()
        msg = (f"자동매매 봇 시작\n"
               f"모드: {'모의투자' if settings.IS_MOCK else '실전'}\n"
               f"조건식: {', '.join(settings.CONDITION_NAMES)}\n"
               f"주문가능: {deposit:,}원\n"
               f"보유: {len(self.strategy_mgr.holdings)}종목 "
               f"(1A={self.strategy_mgr.count_holdings_by_strategy('1A')}, "
               f"눌림={self.strategy_mgr.count_holdings_by_strategy('1A_눌림')})\n"
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

        # 종가베팅 전용 검색식 — 구독은 하되 매매 라우팅에는 넣지 않는다.
        # 이름이 비어있거나 HTS에 없으면 seq가 안 잡히고, 그러면 스캐너가
        # 기존 _cond_names 폴백으로 조용히 되돌아간다(기능 무력화 없음).
        cb_name = getattr(settings, "CLOSING_BET_CONDITION_NAME", "") or ""
        if cb_name:
            cb_seq = name_to_seq.get(cb_name)
            if cb_seq:
                await self.ws.subscribe_condition(cb_seq)
                self._closing_bet_seq = _norm_seq(cb_seq)
                logger.info(
                    "   '%s' -> seq=%s (종가베팅 전용, 매매 라우팅 제외)", cb_name, cb_seq
                )
            else:
                # 이름이 안 맞으면 조용히 0종목이 되는 게 아니라 **폴백**한다.
                # HTS 화면의 [005] 같은 표기는 표시용 인덱스이고 API seq는
                # 그것과 다르다(실측: 화면 [000]/[001]/[002] -> API seq 1/2/3).
                # 그래서 번호가 아니라 반드시 '이름'으로 찾는다.
                logger.warning(
                    "   '%s' 종가베팅 조건식 없음 — 기존 장중 검색식 유니버스로 폴백. "
                    "HTS 조건식 이름과 config.ini CLOSING_BET_CONDITION_NAME이 "
                    "정확히 같은지 확인할 것 (등록된 조건식: %s)",
                    cb_name, sorted(cond_map.values())[:40],
                )

        if not settings.CONDITION_NAMES and settings.CONDITION_NOS:
            for seq in settings.CONDITION_NOS:
                if seq in cond_map:
                    await self.ws.subscribe_condition(seq)

    async def _snapshot_closing_bet_universe(self):
        """종가베팅 전용 검색식의 현재 편입 종목을 한 번 받아 유니버스를 채운다.

        ⚠️ setup() 단계에서만 호출할 것 — fetch_condition_snapshot의 _wait_for가
        소켓에서 직접 recv 하기 때문에, listen()이 돌고 있는 장중에 부르면
        체결틱/호가를 최대 10초 흘린다(15:10까지 손절 감시가 살아있어야 하므로
        치명적). setup()은 asyncio.gather(listen(), ...)보다 먼저 실행된다.

        여기서 받은 종목은 **매매 라우팅에 넣지 않는다** — on_condition_hit을
        부르지 않고 _closing_bet_codes에만 넣는다.
        """
        if not self._closing_bet_seq:
            return
        try:
            codes = await self.ws.fetch_condition_snapshot(self._closing_bet_seq)
        except Exception:
            logger.exception("종가베팅 유니버스 초기 스냅샷 실패(실시간 편입으로 계속 누적)")
            return
        codes = [c for c in (codes or []) if c]
        self._closing_bet_codes.update(codes)
        logger.info(
            "🔔 [종가베팅] 초기 유니버스 %d종목 (seq=%s) %s",
            len(self._closing_bet_codes), self._closing_bet_seq,
            sorted(self._closing_bet_codes)[:10],
        )

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
        """버퍼에 남은(3개 미만) 종목을 SUB_FLUSH_SEC 후 발사.

        (2026-08-02) try/except 추가 — 이 루프는 **1초마다** 도는데다 WS
        전송을 건드린다. 여기서 예외가 하나라도 새어나가면 asyncio.gather()가
        통째로 무너져 봇이 종료된다(2026-07-27 실제 장애와 같은 붕괴 경로).
        _flush_subscribe가 자체 예외처리를 갖고 있어 지금은 안전하지만,
        나머지 16개 태스크와 같은 방어 패턴을 맞춰 둔다.
        """
        while not self._stop:
            await asyncio.sleep(1)
            try:
                async with self._sub_buffer_lock:
                    if (self._sub_buffer
                            and time.time() - self._last_buffer_add >= self.SUB_FLUSH_SEC):
                        batch = self._sub_buffer
                        self._sub_buffer = []
                        await self._flush_subscribe(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("구독 플러시 예외")

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
        # ── 종가베팅 전용 검색식은 매매 경로에 절대 들어가지 않는다 ──────
        # (2026-08-02) 여기서 **가장 먼저** 갈라낸다. 아래 기존 흐름은 한 줄도
        # 건드리지 않으므로 장중 1A/Pullback 진입 로직에 영향이 없다.
        # CONDITION_NAMES에 그냥 넣으면 resolve_strategy의 "둘 다 아님 -> 1A"
        # 폴백에 걸려 장중 매수 후보가 되어버린다.
        if self._closing_bet_seq and _norm_seq(cond_seq) == self._closing_bet_seq:
            if signal_type == "I":
                self._closing_bet_codes.add(stock_code)
            else:
                # 이탈 — 14:50 시점 멤버십이 정확해야 하므로 빼준다.
                # (조건식의 등락률/고가대비/볼밴은 장중 계속 변하므로
                #  '하루 동안 한 번이라도 걸린 종목'을 누적하면 안 된다.)
                self._closing_bet_codes.discard(stock_code)
            logger.info(
                "🔔 종가베팅 검색식 %s: %s (현재 %d종목, 매매 라우팅 제외)",
                "편입" if signal_type == "I" else "이탈",
                stock_code, len(self._closing_bet_codes),
            )
            return

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
            # (2026-08-05) try/except를 **루프 안으로** 옮겼다. 예전엔 루프
            # 바깥이라 한 종목에서 예외가 나면 거기서 루프가 끊겨 **뒤에
            # 남은 보유 종목들이 그 사이클을 통째로 건너뛰었다.** 이 태스크는
            # 틱이 끊긴 종목의 손절을 받아주는 마지막 안전망이라, 한 종목의
            # 문제가 다른 종목의 손절을 막으면 안 된다.
            for code in list(self.strategy_mgr.holdings.keys()):
                try:
                    candles = await asyncio.to_thread(
                        self.rest.get_minute_candles, code, interval=1, count=1
                    )
                    if candles:
                        self.strategy_mgr.on_price_update(code, candles[0]["close"])
                except Exception:
                    logger.exception("[%s] 보유 종목 가격 폴링 예외 — 다음 종목 계속", code)

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
            if not buy_time:
                return False
            elapsed = (now - buy_time).total_seconds()
            if elapsed < RECONCILE_GRACE_SECONDS:
                return True
            # (2026-08-12) 서버에서 **한 번도 확인된 적 없는** 포지션은 아직
            # 잔고 반영 전일 수 있다(실측 99초). 상한까지는 더 기다린다.
            # `seen_on_server`는 아래 루프가 server_qty>0을 볼 때 찍는다.
            if (not pos.get("seen_on_server")
                    and elapsed < RECONCILE_UNSEEN_GRACE_SECONDS):
                return True
            return False

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

            # (2026-08-11) 서버에서 실물을 **한 번이라도 확인**했다는 표식.
            # `_release_ghost_position`이 '진짜 미체결'과 '사용자 수동매도'를
            # 가르는 유일한 근거다 — 08-11에 사용자 매도 6건이 전부
            # "미체결 포지션 정리"로 기록돼 실현손익 통계가 오염됐다.
            if server_qty > 0:
                pos["seen_on_server"] = True

            # ── 수동 추가매수 합산 (2026-08-11 사용자 지정) ──────────────
            # 서버 수량이 **늘었으면** 사용자가 HTS에서 직접 더 산 것이다.
            # 예전엔 이 방향을 통째로 무시해서, 봇은 자기 수량만 팔고
            # 추가분은 계좌에 남아 아무도 관리하지 않았다(손절·15:10 대상 밖).
            # 평단은 키움이 이미 계산해 준 값(avg_price = pur_pric)을 쓴다 —
            # 수동 체결가를 몰라도 된다.
            # 🔴 (2026-08-12 결함수정) 여기가 `elif`였다. 바로 위 분기가
            #    `if server_qty > 0`이라, 수량이 0보다 크면 **항상** 위에서
            #    끝나고 이 블록은 영원히 실행되지 않았다(0이면 `0 > tracked`도
            #    거짓이라 어느 쪽으로도 도달 불가). 즉 08-11에 만든 이 기능은
            #    **한 번도 동작한 적이 없다.**
            #    실측(08-12): JW신약 봇 515주 vs 실계좌 1,032주 —
            #    사용자가 직접 산 517주를 봇이 모른 채 방치했다.
            if server_qty > tracked_qty:
                # ⚠️ 봇 자신의 분할 2차/추가매수가 체결됐는데 아직 기록 전인
                #    15초 틈을 '수동매수'로 오인하면 안 된다. 두 가지로 막는다:
                #    ① 되돌림 계획이 살아있으면(봇이 더 살 수 있는 상태) 보류
                #    ② **2회 연속 같은 값**으로 관측될 때만 반영
                if code in getattr(strat, "_entry_plans", {}):
                    continue
                prev = pos.get("_pending_qty_up")
                if prev != server_qty:
                    pos["_pending_qty_up"] = server_qty
                    continue
                pos.pop("_pending_qty_up", None)

                avg = float(server_info.get("avg_price") or 0)
                old_qty, old_avg = tracked_qty, pos.get("buy_price")
                pos["qty"] = server_qty
                if avg > 0:
                    pos["buy_price"] = avg
                    # 기준가가 바뀌면 본전스톱 상태를 재설정한다. 안 하면 옛
                    # 평단 기준 고점이 남아, 평단이 **올라간** 경우 순손익이
                    # 뚝 떨어져 본전스톱이 즉시 오발동한다.
                    pos["breakeven_armed"] = False
                    pos["breakeven_peak"] = 0.0
                # ⚠️ origin_price는 **유지**한다 — 추가매수 최종손절(-6%)의
                #    기준이 '원가'이기 때문이다(평단이 아니다).
                name = pos.get("stock_name", code)
                logger.warning(
                    "[%s] %s 💰 수동 추가매수 감지 — %d주 -> %d주 / 평단 %s -> %s",
                    code, name, old_qty, server_qty,
                    f"{old_avg:,.0f}" if old_avg else "?",
                    f"{avg:,.0f}" if avg else "(평단 미상, 유지)",
                )
                trade_id = pos.get("trade_id")
                if trade_id and avg > 0:
                    try:
                        TradeRepository.update(trade_id, {
                            "buy_price": avg,
                            "buy_quantity": server_qty,
                            "buy_amount": avg * server_qty,
                        })
                    except Exception:
                        logger.warning("[%s] 수동 추가매수 DB 갱신 실패", code)
                if send_telegram:
                    send_telegram(
                        f"💰 수동 추가매수 반영\n{name} ({code})\n"
                        f"{old_qty}주 -> {server_qty}주 / 평단 "
                        f"{avg:,.0f}원\n봇이 합산 수량으로 청산합니다.",
                        target="order",
                    )
                continue

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

        self._detect_orphan_positions(server_positions)

    def _detect_orphan_positions(self, server_positions: dict):
        """서버엔 있는데 봇 holdings엔 없는 종목 감지 (2026-08-01 신규 안전망).

        기존 _reconcile_manual_sells는 holdings -> 서버 방향만 봤다. 그래서
        **매도 주문이 접수는 됐는데 체결이 안 된 경우**(지정가 시절의 실제 위험),
        봇은 holdings에서 지우고 "매도 체결" 알림까지 보내지만 주식은 계좌에
        그대로 남아 아무도 관리하지 않는 상태가 됐다. 손절도 익절도 안 되고
        장마감 강제청산 대상도 아니다(강제청산은 holdings를 순회하므로).

        매도를 시장가로 바꿔 발생 확률 자체를 크게 낮췄지만, 그래도 남는
        경우(부분체결, 수동 매수, API 이상)를 놓치지 않으려고 감지만 해서
        알린다. **자동 복구는 하지 않는다** — 사용자가 직접 산 종목까지 봇이
        멋대로 관리 대상에 넣어 팔아버리면 더 큰 사고이므로, 판단은 사람에게
        맡기는 것이 맞다. 알림은 종목당 1회만(장중 반복 스팸 방지).
        """
        strat = self.strategy_mgr
        if not strat or not server_positions:
            return
        for code, info in server_positions.items():
            if code in strat.holdings or code in strat.pending:
                continue
            if code in self._orphan_notified:
                continue
            qty = (info or {}).get("qty", 0)
            if qty <= 0:
                continue
            self._orphan_notified.add(code)
            name = strat._stock_names.get(code, code)
            logger.warning(
                "[%s] %s 유령 포지션 감지 — 서버 잔고 %d주인데 봇 관리 목록에 없음",
                code, name, qty,
            )
            send_telegram(
                f"⚠️ 미관리 잔고 감지\n{name} ({code}) {qty}주\n"
                f"봇이 '매도 완료'로 처리했지만 계좌에 남아 있습니다.\n"
                f"(매도 미체결 또는 수동 매수 가능성 — 자동 청산 대상이 아니므로 "
                f"직접 확인 필요)",
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
                    f"눌림={self.strategy_mgr.count_holdings_by_strategy('1A_눌림')})",
                    f"감시 중(틱 수집): {len(self.phase1b_ctrl.watched)}종목",
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
        # ── 시간 기반 자동청산 전면 OFF (2026-08-12 사용자 지정) ──────────
        # 🔴 완료 플래그를 **즉시** 세운다. task_auto_shutdown이
        #    `_force_close_done`을 종료 조건으로 보기 때문에, 그냥 return만
        #    하면 이 값이 영영 False로 남아 봇이 15:40 하드 폴백까지 살아
        #    있는다(정기보고·WS재연결이 그때까지 계속 돈다).
        if not FORCE_CLOSE_ENABLED:
            self._force_close_done = True
            logger.info(
                "⏸ 15:10 강제청산 **비활성** (FORCE_CLOSE_ENABLED=False) — "
                "보유분은 가격 기반 청산(손절/익절/본전스톱/VI)에만 반응하고, "
                "남은 종목은 오버나이트로 넘어간다. 다음 기동 때 manual로 격리된다."
            )
            return

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
                    # 기준가 확보: 실시간 체결가 -> 분봉 순 (2026-08-02).
                    # 예전엔 분봉(REST) 하나만 썼는데, 이 시각은 429가 가장
                    # 심한 구간이라 조회가 실패하면 `if candles:`에 걸려
                    # **_execute_sell이 아예 안 불리고 포지션이 오버나이트로
                    # 넘어갔다**(장 마감 후엔 재시도할 태스크도 없다).
                    # 매도는 시장가라 기준가는 기록/로깅용이므로, 실시간
                    # 체결가만 있어도 청산을 진행하는 게 훨씬 안전하다.
                    px = 0
                    try:
                        px = self.strategy_mgr._fresh_tick_price(code, max_age_sec=600) or 0
                    except Exception:
                        px = 0
                    if not px:
                        try:
                            candles = self.rest.get_minute_candles(code, interval=1, count=1)
                            if candles:
                                px = candles[0]["close"]
                        except Exception:
                            logger.exception(f"[{code}] 강제청산 기준가 조회 실패")
                    if not px:
                        px = self.strategy_mgr.holdings.get(code, {}).get("buy_price", 0)
                        logger.warning(
                            "[%s] 강제청산 기준가를 못 구해 매수가로 대체 — 시장가로 청산 진행",
                            code,
                        )
                    try:
                        if px:
                            self.strategy_mgr._execute_sell(code, px, "장마감 강제청산")
                        else:
                            logger.error("[%s] 강제청산 불가 — 기준가 전무", code)
                    except Exception:
                        logger.exception(f"[{code}] 강제청산 실패")
                await asyncio.sleep(60)

            # 완료 판정은 트리거 블록 **밖**에서 매 루프 다시 본다 (2026-08-03).
            # 안에 두면 위 블록이 하루 1회만 실행되므로, 그 순간 매도가 아직
            # pending이라 holdings가 안 비어 있으면 플래그를 영영 못 세운다.
            # 보유가 남아 있으면 완료로 치지 않는다 — 오버나이트 방지가 최우선이라
            # 그때는 15:40 하드 폴백까지 봇을 살려 둔다.
            if triggered and not self.strategy_mgr.holdings:
                self._force_close_done = True
            await asyncio.sleep(10)

    async def task_auto_shutdown(self):
        # 2026-07-27엔 daily_backtest(15:30 트리거)와 순서가 겹쳐서(15:20에 먼저
        # 종료되면 백테스트가 못 돔) 통째로 비활성화했었는데, 그 뒤로 재활성화를
        # 안 해서 2026-07-30 실전에서 장마감(15:15 강제청산) 이후에도 정기보고/
        # WS재연결/조건재등록이 19:32까지 4시간 넘게 계속 돌아간 문제 발생.
        # daily_backtest가 15:30에 트리거돼 보통 30~60초 안에 끝나는 걸 감안해
        # 15:40으로 늦춰서 재활성화(2026-07-30) — 강제청산(15:15)과 백테스트
        # 리포트(15:30) 둘 다 끝난 뒤에만 종료되도록.
        # (2026-08-03) 고정 시각 대기 -> **할 일이 끝나면 즉시 종료**로 변경.
        # 마무리 3종(14:50 종가베팅 / 15:10 강제청산 / 15:30 백테스트)이 모두
        # 끝나면 기다리지 않고 내린다. 08-03에 15:10 청산이 끝난 뒤에도 테마
        # 재계산과 조건검색 수신이 15:40까지 계속 돌았다.
        #
        # ⚠️ 종가베팅(14:50)만으로 내리면 안 된다 — 그 뒤에 **강제청산(15:10)**이
        # 있어서, 먼저 종료하면 보유 종목이 그대로 오버나이트로 넘어간다.
        # 그래서 "종가베팅 완료"가 아니라 "셋 다 완료"가 조건이다.
        # 15:40은 그대로 두되 이제는 **하드 폴백**이다(어느 태스크가 멈춰도
        # 봇이 밤새 도는 일은 없게).
        target_time = "15:40"
        while not self._stop:
            now_str = datetime.now().strftime("%H:%M")
            all_done = (
                self._closing_bet_done
                and self._force_close_done
                and self._backtest_done
            )
            if all_done or now_str >= target_time:
                if all_done:
                    msg = (
                        "⏰ 장마감 마무리 완료(종가베팅·강제청산·일일리포트) — "
                        "프로그램을 안전하게 자동 종료합니다."
                    )
                else:
                    msg = (
                        f"⏰ 설정된 시간({target_time}) 도달. 매매를 중지하고 "
                        f"프로그램을 안전하게 자동 종료합니다. "
                        f"(미완료: "
                        f"{'종가베팅 ' if not self._closing_bet_done else ''}"
                        f"{'강제청산 ' if not self._force_close_done else ''}"
                        f"{'백테스트' if not self._backtest_done else ''})"
                    )
                logger.info(msg)
                # 텔레그램 실패가 종료 절차를 막지 않게 한다 (2026-08-02) —
                # 여기서 예외가 나면 gather가 무너져 shutdown()이 안 불린다.
                try:
                    send_telegram(msg, target="signal")
                except Exception:
                    logger.exception("자동종료 알림 실패(종료는 계속 진행)")

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
                # 대상 유니버스 (2026-08-02 변경):
                #   1순위 — 종가베팅 전용 검색식의 **현재** 멤버십
                #   폴백  — 기존처럼 오늘 장중 검색식에 걸렸던 종목 전체
                #
                # 전용 검색식으로 바꾼 이유(실측): 기존 유니버스는 장중 급등/눌림
                # 스크린의 출력이라 오버나이트 보유와 성질이 반대다. 07-28~31
                # 실제 후보 6종목 중 5개가 코스닥 중소형이었고, 그중 폴라리스AI는
                # 장중엔 저유동성으로 걸러내던 종목이다. 특히 '눌림목자동'은
                # 정의상 고가 대비 되돌린 종목이라, 종가베팅이 원하는 '고가 근처
                # 마감'과 정면으로 어긋난다.
                # 폴백을 남기는 이유: HTS에 검색식을 아직 안 만들었거나 이름이
                # 바뀌면 seq가 안 잡히는데, 그때 조용히 0종목이 되면 안 된다.
                universe_src = "종가베팅 전용 검색식"
                target_codes = sorted(self._closing_bet_codes)
                if not target_codes:
                    universe_src = "폴백(장중 검색식 전체)"
                    target_codes = list(strat._cond_names.keys())
                    if self._closing_bet_seq:
                        # 검색식은 정상 등록됐는데 하루 종일 편입이 0건이라면
                        # (a) 조건이 너무 빡빡하거나 (b) 구독/파싱이 깨진 것이다.
                        # 조용히 폴백만 하면 전환이 안 된 걸 눈치채지 못하므로
                        # 텔레그램으로 올린다.
                        warn = (
                            f"⚠️ 종가베팅 검색식(seq={self._closing_bet_seq})이 "
                            f"등록됐는데 오늘 편입 0건 — 조건이 과하거나 구독 이상. "
                            f"장중 검색식 유니버스로 폴백합니다."
                        )
                        logger.warning(warn)
                        if send_telegram:
                            send_telegram(warn, target="closing_bet")
                    else:
                        logger.warning(
                            "⚠️ 종가베팅 검색식 미등록 — config.ini의 "
                            "CLOSING_BET_CONDITION_NAME과 HTS 조건식 이름을 확인할 것"
                        )
                logger.info(
                    "🔔 [종가베팅] 유니버스 %d종목 — %s", len(target_codes), universe_src
                )

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
                    "🔔 [종가베팅] 평가 완료 %d종목 / 적격 %d종목 (%s, 캐시 %d파일 %.1fMB)",
                    len(target_codes), len(candidates), universe_src,
                    st["files"], st["bytes"] / 1024 / 1024,
                )

                ranked = sorted(
                    candidates.items(), key=lambda x: x[1]["closing_score"], reverse=True
                )[:10]

                if not ranked:
                    # (2026-08-05) 예전엔 여기서 조용히 continue라 **아무 알림도
                    # 안 갔다.** 그러면 다윤님 입장에서 "스캔이 안 돌았다"와
                    # "돌았는데 후보가 없다"를 구분할 수 없다 — 이 프로젝트가
                    # 진단 알림에서 계속 지켜온 원칙(정상 필터링 ↔ 코드 이상 분리)과
                    # 정면으로 어긋나는 지점이었다. 08-05에 실제로 이것 때문에
                    # "종가베팅이 왜 안 왔지?"를 로그로 파고들어야 했다.
                    # 후보가 0건인 것도 결과이므로 퍼널을 실어 보낸다.
                    msg = (
                        f"🔔 종가베팅 후보 없음\n"
                        f"유니버스 {len(target_codes)}종목 평가 완료 — 적격 0종목\n"
                        f"({universe_src})"
                    )
                    logger.info(msg)
                    if send_telegram:
                        send_telegram(msg, target="closing_bet")
                    continue

                # 헤더에 실제 건수를 쓴다 — "TOP 10"으로 고정돼 있었는데 실측
                # 산출은 하루 1~2건이라(07-28 1 / 07-29 1 / 07-30 2 / 07-31 2)
                # 제목만 보고 10종목이 나온 줄 오해하기 쉬웠다.
                lines = [f"🔔 종가베팅 후보 {len(ranked)}종목 (유니버스 {len(target_codes)})", ""]
                for i, (code, r) in enumerate(ranked, 1):
                    # 종가베팅 전용 검색식 종목은 on_condition_hit을 안 거치므로
                    # strat._stock_names에 이름이 없다. 여기서 1회 조회한다
                    # (하루 1회, 최대 10종목이라 REST 부담 없음).
                    name = strat._stock_names.get(code) or self._fetch_stock_name(code)
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
            finally:
                # 실패해도 '이 작업은 오늘 끝났다'로 본다 — 여기서 예외가 났다고
                # 종료를 무한정 미루면 봇이 밤새 도는 옛 문제로 되돌아간다.
                # (실행 자체가 안 된 경우는 done_date 가드가 이미 막고 있다.)
                self._closing_bet_done = True

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
            finally:
                # 실패해도 완료로 본다 — 리포트는 부가 기능이라 이것 때문에
                # 봇이 계속 떠 있을 이유가 없다(종료 조건 주석 참고).
                self._backtest_done = True

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
        그때 슬롯이 꽉 차 있으면 이후 슬롯이 비어도 재시도가 안 되던 문제 수정.
        (2026-08-02) 진입이 틱 구동으로 바뀌면서 主 경로에서 **안전망으로 강등**
        됐다 — 평시엔 on_trade의 _maybe_tick_entry가 그 틱에서 바로 발화한다."""
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

    async def task_fid228_watchdog(self):
        """FID 228(키움 공식 체결강도) 수신 감시 — 09:05 1회 (2026-08-02 신규).

        이번 개편에서 **유일하게 실거래로 검증되지 않은 가정**이 "체결 틱에
        FID 228이 실려 온다"는 것이다. 파싱 코드는 예전부터 있었지만 1L을
        주석처리한 뒤로 아무도 쓰지 않아 값이 실제로 오는지 확인된 적이 없다.

        228이 안 오면 무장(강도 100 이상 연속 유지)이 영원히 성립하지 않고,
        그 결과 **하루 종일 매수 0건**이 된다. 그런데 겉으로는 아무 에러도
        나지 않아서(조용히 탈락할 뿐) 로그를 직접 뒤지기 전엔 알 수 없다.

        진단 알림(30분 주기)에도 같은 경고가 들어가지만, 이건 **개장 직후
        한 번 즉시** 알려서 장 초반에 손쓸 수 있게 하려는 별도 장치다.
        정상이면 조용히 통과 로그만 남기고 끝난다.
        """
        notified = False
        while not self._stop:
            await asyncio.sleep(30)
            try:
                now = datetime.now()
                if notified or not time_in(now, 9, 5):
                    continue
                if time_after(now, 14, 50):
                    continue
                strat = self.strategy_mgr
                if not strat:
                    continue
                notified = True

                total = getattr(strat, "_trade_tick_total", 0)
                seen = len(getattr(strat, "_fid228_seen", ()))
                watched = len(getattr(strat.phase1b, "watched", ())) if strat.phase1b else 0

                if total == 0:
                    msg = (
                        "🚨 [긴급] 09:05 기준 체결틱(0B) 수신 0건\n"
                        f"감시 종목 {watched}개\n"
                        "실시간 구독이 안 됐을 가능성 — 로그의 '실시간 등록:' 확인 필요.\n"
                        "이 상태면 오늘 매수가 한 건도 나가지 않습니다."
                    )
                elif seen == 0:
                    msg = (
                        "🚨 [긴급] 체결강도(FID 228)가 한 번도 안 옴\n"
                        f"체결틱은 {total:,}건 정상 수신 / 감시 {watched}종목\n"
                        "진입 무장이 228에만 걸려 있어 **오늘 매수 0건**이 됩니다.\n"
                        "확인: 로그의 '🔑 0B 체결 raw 키'에 '228'이 있는지.\n"
                        "없으면 kiwoom_ws.py의 FID 매핑을 고쳐야 합니다."
                    )
                else:
                    logger.info(
                        "✅ FID 228 수신 정상 — 체결틱 %s건 / 228 수신 %d종목 / 감시 %d종목",
                        f"{total:,}", seen, watched,
                    )
                    continue

                logger.error(msg.replace("\n", " | "))
                try:
                    send_telegram(msg, target="signal")
                except Exception:
                    logger.exception("FID228 경고 발송 실패")
            except Exception:
                logger.exception("FID228 감시 예외")

    @staticmethod
    def _is_remote_control_running() -> bool:
        """claude --remote-control 프로세스 존재 여부 확인 (블로킹 — 반드시
        asyncio.to_thread로 호출할 것, 메인 루프를 막으면 안 됨).

        Win32_Process 커맨드라인을 훑어 '--remote-control' 문자열을 찾는다.
        확인 자체가 실패하면(powershell 오류 등) 오탐 알림을 막기 위해
        '있다'(True)로 간주한다 — 이 워치독의 목적은 진짜 이상만 잡는 것.
        """
        try:
            result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\").CommandLine",
                ],
                capture_output=True, text=True, timeout=15,
            )
            return "--remote-control" in (result.stdout or "")
        except Exception:
            logger.exception("원격제어 프로세스 확인 중 오류")
            return True

    async def task_remote_control_watchdog(self):
        """모바일 원격제어(claude --remote-control) 기동 감시 — 09:06 1회 (2026-08-02 신규).

        발단: 모바일 앱에서 원격제어 세션 '종료'를 누르는 과정(특히 실수로
        두 번 누르는 경우)에서 세션이 비정상 종료되면, start_remote_control.ps1
        의 재시작 루프가 있어도 연속 실패로 자동재시작을 포기하거나 PATH/인증
        문제로 아예 못 뜨는 극단적 경우가 있을 수 있다. 원격제어는 트레이딩
        봇(main.py)과 완전히 별도 프로세스/창이라 **매매 자체엔 영향이 없지만**,
        "오늘 장중에 폰으로 확인을 못 한다"는 사실을 그날 안에 알아야 대응할
        수 있다. FID228 워치독과 동일한 패턴 — 09:0X에 1회만 확인, 평소엔
        조용히 로그만 남기고 끝난다.
        """
        notified = False
        while not self._stop:
            await asyncio.sleep(30)
            try:
                now = datetime.now()
                if notified or not time_in(now, 9, 6):
                    continue
                if time_after(now, 14, 50):
                    continue
                notified = True

                running = await asyncio.to_thread(self._is_remote_control_running)
                if running:
                    logger.info("✅ 원격제어(claude --remote-control) 프로세스 정상 확인")
                    continue

                msg = (
                    "⚠️ [원격제어] claude --remote-control 프로세스가 안 보입니다.\n"
                    "모의투자 봇 매매엔 영향 없음(완전 별도 프로세스) — 다만 오늘\n"
                    "폰 원격 접속이 안 될 수 있습니다.\n"
                    "PC에서 확인: start_remote_control.ps1 수동 재실행 필요할 수 있음."
                )
                logger.warning(msg.replace("\n", " | "))
                try:
                    send_telegram(msg, target="signal")
                except Exception:
                    logger.exception("원격제어 워치독 알림 발송 실패")
            except Exception:
                logger.exception("원격제어 워치독 예외")

    async def task_missed_opportunities(self):
        """'놓친 기회' 알림 (2026-08-05 신규).

        task_entry_diagnostics가 **사유별 종목 수**를 보여준다면, 이건
        **어떤 종목을 얼마나 아깝게 놓쳤는지**를 계층으로 보여준다.
        다윤님이 폰으로 보고 수동 매수를 판단하기 위한 것이다.

        계층 분리가 핵심이다 — 08-05 실측에서 탈락의 대부분이 '등락률 상한
        초과'(2,222회)였는데 그건 아까운 게 아니라 의도적 배제였다. 반면
        '되돌림 미도달'은 무장·버스트·등락률을 전부 통과한 완벽한 신호라
        성격이 정반대다. 한 덩어리로 보여주면 판단이 불가능하다.

        10분 주기 고정 — 이 알림은 '지금 살까?'를 묻는 것이라 늦으면 무의미하다.
        """
        MISS_INTERVAL = 600
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
                if time.time() - last_sent < MISS_INTERVAL:
                    continue
                body = await asyncio.to_thread(strat.build_missed_opportunities)
                # 놓친 게 없으면 보내지 않는다 — 조용한 게 정상이고,
                # 빈 알림이 10분마다 오면 정작 중요한 것이 묻힌다.
                if "아깝게 놓친 후보 없음" in body:
                    last_sent = time.time()
                    continue
                send_telegram(body, target="signal")
                last_sent = time.time()
            except Exception:
                logger.exception("놓친 기회 알림 실패")

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
                self.task_missed_opportunities(),
                self.task_fid228_watchdog(),
                self.task_remote_control_watchdog(),
                # (2026-08-07 재활성화) 실시간 push가 유일한 수집 경로였고
                # push를 놓치면 복구 수단이 아예 없었다. 장중에 못 켰던 이유는
                # fetch_condition_snapshot이 소켓을 **직접** recv 해서 그 사이
                # 체결틱/호가/조건편입을 최대 10초 통째로 버렸기 때문인데,
                # kiwoom_ws._wait_for를 listen() 경유(future 디스패치)로 바꿔
                # 유실 0으로 만들었다. 상세는 그쪽 주석 참고.
                self.task_condition_snapshot_poll(),
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
                f"눌림={self.strategy_mgr.count_holdings_by_strategy('1A_눌림')})"
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
