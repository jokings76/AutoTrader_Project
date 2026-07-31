"""
키움 WebSocket 클라이언트 (확장판)
─────────────────────────────────────
역할:
  - WS 연결, 로그인, PING 자동 응답, 자동 재연결
  - 조건검색 실시간 (CNSRREQ search_type=1): 편입/이탈 신호 콜백
  - 조건검색 즉시 검색 (CNSRREQ search_type=0): 현재 진입 종목 스냅샷
  - 종목별 실시간 (REG): 주식체결(0B) + 주식호가잔량(0D) 콜백
  - 재접속 시 조건식 + 실시간 자동 재등록
  - REG 구독 사이 sleep으로 빈도 제한 회피
  - ★ half-open(좀비 연결) 감지: IDLE_TIMEOUT 무수신 시 ping 확인 → 실패하면 재연결
  - ★ 블로킹 방지: 콜백(async/sync 자동 판별)을 백그라운드로 처리
"""
import asyncio
import json
import random
import time
import websockets

from config import settings
from utils.logger import logger


TYPE_TRADE = "0B"
TYPE_ORDERBOOK = "0D"
# 종목프로그램매매 실시간 타입 (2026-07-31 신규) — FID 118/119/120/122/123/124.
# 주의: 이 타입 코드값("0g")은 문서 확인 기준 추정치이고 이 프로젝트에서 실제
# 구독으로 검증된 적은 아직 없다. 장 시작 후 아래 진단 로그("🔑 0g 프로그램매매
# raw 키")가 한 번이라도 찍히면 맞는 것 — 안 찍히면(0B/0D는 계속 찍히는데 이것만
# 조용하면) 타입 코드가 틀린 것이므로 재확인 필요.
TYPE_PROGRAM = "0g"

# WS REG 빈도 제한 회피 (키움 정책)
REG_INTERVAL_SEC = 0.3

# half-open(좀비 연결) 감지: 이 시간(초) 동안 무수신이면 연결 점검 → 재연결
IDLE_TIMEOUT = 60


class KiwoomWS:
    MOCK_URL = "wss://mockapi.kiwoom.com:10000/api/dostk/websocket"
    REAL_URL = "wss://api.kiwoom.com:10000/api/dostk/websocket"

    def __init__(
        self,
        token: str,
        condition_manager,  # 👈 여기 추가!
        is_mock: bool = True,
        on_signal=None,
        on_trade=None,
        on_orderbook=None,
        on_program=None,
        on_disconnect=None,
        on_reconnect=None,
    ):
        self.token = token
        self.condition_manager = condition_manager  # 👈 여기 추가! (초기화)
        self.url = self.MOCK_URL if is_mock else self.REAL_URL
        self.on_signal = on_signal
        self.on_trade = on_trade
        self.on_orderbook = on_orderbook
        self.on_program = on_program  # 종목프로그램매매(0g) 콜백 (2026-07-31)
        self.on_disconnect = on_disconnect  # 단절 감지 직후 1회 호출 (인자 없음)
        self.on_reconnect = on_reconnect  # 재연결+재구독 완료 직후 1회 호출(outage_seconds: float)

        # ... (이후 기존 코드 유지)

        self.ws = None
        self.connected = False
        self._stop = False
        self._ever_connected = False
        self._disconnected_at: float | None = None

        self._subscribed_seqs: list[str] = []
        self.condition_map: dict[str, str] = {}
        self._subscribed_realtime: dict[str, set[str]] = {}
        self._cond_keys_logged = False
        self._orderbook_keys_logged = False
        self._program_keys_logged = False
        self._real_empty_logged = False   # 진단용 (2026-07-31)
        self._seen_item_types: set = set()  # 진단용 (2026-07-31)

        # REG 호출 직렬화 + 빈도 제한
        self._reg_lock = asyncio.Lock()
        self._last_reg_ts = 0.0

    # ─────────────────────────────────────────
    # 1. 연결 & 로그인
    # ─────────────────────────────────────────
    async def connect(self):
        logger.info(f"🔌 WebSocket 연결 시도: {self.url}")
        self.ws = await websockets.connect(self.url, ping_interval=None)

        login_msg = {"trnm": "LOGIN", "token": self.token}
        await self.ws.send(json.dumps(login_msg))

        raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
        resp = json.loads(raw)

        if resp.get("trnm") != "LOGIN":
            raise RuntimeError(f"LOGIN 응답이 아닌 메시지 수신: {resp}")
        if resp.get("return_code") != 0:
            raise RuntimeError(
                f"LOGIN 실패: code={resp.get('return_code')} msg={resp.get('return_msg')}"
            )

        self.connected = True
        logger.info("✅ WebSocket 로그인 성공")

    # ─────────────────────────────────────────
    # 2. 조건식 목록 조회
    # ─────────────────────────────────────────
    async def fetch_condition_list(self) -> dict[str, str]:
        await self._send({"trnm": "CNSRLST"})
        resp = await self._wait_for("CNSRLST", timeout=10)

        items = resp.get("data") or []
        result = {}
        for it in items:
            if isinstance(it, list):
                seq, name = str(it[0]), str(it[1])
            elif isinstance(it, dict):
                seq = str(it.get("seq") or it.get("Seq") or "")
                name = str(it.get("name") or it.get("Name") or "")
            else:
                continue
            if seq:
                result[seq] = name

        self.condition_map = result
        logger.info(f"📋 조건식 목록 {len(result)}개 조회 완료")
        for seq, name in result.items():
            logger.info(f"   seq={seq}  name={name}")

        if not result:
            logger.warning(
                "⚠️ 조건식이 0개입니다! 영웅문4 [0150]에서 조건식을 만들고 "
                "'내 조건식 저장'을 눌렀는지 확인하세요."
            )
        return result

    # ─────────────────────────────────────────
    # 3. 조건식 실시간 등록/해제
    # ─────────────────────────────────────────
    async def subscribe_condition(self, seq: str, stex_tp: str = "K"):
        seq = str(seq)
        msg = {
            "trnm": "CNSRREQ", "seq": seq, "search_type": "1",
            "stex_tp": stex_tp, "cont_yn": "N", "next_key": "",
        }
        await self._send(msg)
        if seq not in self._subscribed_seqs:
            self._subscribed_seqs.append(seq)
        name = self.condition_map.get(seq, "?")
        logger.info(f"📡 조건식 실시간 등록: seq={seq} ({name})")

    async def unsubscribe_condition(self, seq: str):
        seq = str(seq)
        await self._send({"trnm": "CNSRCLR", "seq": seq})
        if seq in self._subscribed_seqs:
            self._subscribed_seqs.remove(seq)
        logger.info(f"📴 조건식 실시간 해제: seq={seq}")

    # ─────────────────────────────────────────
    # 조건식 즉시 검색 (현재 진입 종목 스냅샷)
    # ─────────────────────────────────────────
    async def fetch_condition_snapshot(
        self, seq: str, stex_tp: str = "K", timeout: int = 10, _retry: bool = True,
    ) -> list[str]:
        """조건식 즉시검색(search_type=0) 스냅샷 조회.

        (2026-07-31) return_code==0인데도 return_msg에 에러가 실려오는 응답을
        1회 재시도한다 — 실거래에서 돌파자동매매용(seq=2)이 이렇게 빈 데이터로
        응답받아 그 세션 내내(재시작 전까지) 후보 종목이 전부 누락된 사고가
        있었다(마키나락스/씨피시스템/HD현대에너지솔루션 등은 다른 조건식과
        겹쳐서 우연히 살아남았지만, 그 조건식에만 걸리는 다른 종목들은 그날
        끝까지 완전히 누락됨 — 사용자가 HTS 화면으로 직접 확인). 기존 코드는
        return_code만 보고 return_msg는 확인하지 않아 이 실패를 조용히
        '0종목'으로 취급했다."""
        seq = str(seq)
        await self._send({
            "trnm": "CNSRREQ", "seq": seq, "search_type": "0",
            "stex_tp": stex_tp, "cont_yn": "N", "next_key": "",
        })

        try:
            resp = await self._wait_for("CNSRREQ", seq=seq, timeout=timeout)
        except RuntimeError as e:
            logger.warning(f"⚠️ 조건식 스냅샷 [seq={seq}] 응답 없음: {e}")
            return []

        if _retry and resp.get("return_msg") and not (resp.get("data") or []):
            logger.warning(
                f"⚠️ 조건식 스냅샷 [seq={seq}] 에러 응답('{resp.get('return_msg')}') "
                f"-> 1.5초 후 1회 재시도"
            )
            await asyncio.sleep(1.5)
            return await self.fetch_condition_snapshot(
                seq, stex_tp=stex_tp, timeout=timeout, _retry=False
            )

        logger.info(f"🔍 CNSRREQ 응답원본 [seq={seq}]: {str(resp)[:500]}")
        # [수정] 159번 라인 아래에 추가
        logger.info(f"📸 CNSRREQ 응답원본 [seq={seq}]: {str(resp)[:500]}")

        # --- [추가 시작] ---
        try:
            # 1. 데이터에서 종목코드 리스트 추출 (A 제거)
            jm_list = [item["jmcode"].replace("A", "") for item in (resp.get("data") or [])]
            # 2. 조건식 이름 가져오기
            name = self.condition_map.get(seq, "알수없음")

            # 3. ConditionManager 인스턴스에 전달 (self에 연결되어 있다고 가정)
            if hasattr(self, "condition_manager") and self.condition_manager:
                self.condition_manager.update_snapshot(seq, name, jm_list)
        except Exception as e:
            logger.error(f"스냅샷 매핑 처리 실패: {e}")
        # --- [추가 끝] ---

        data = resp.get("data") or []
        if isinstance(data, dict):
            data = [data]

        codes = []
        for item in data:
            code = (
                item.get("9001") or item.get("jmcode") or item.get("stk_cd") or ""
            ).strip()
            if code.startswith("A"):
                code = code[1:]
            if code:
                codes.append(code)

        name = self.condition_map.get(seq, "?")
        logger.info(f"📸 조건식 스냅샷 [seq={seq} {name}]: {len(codes)}종목 → {codes}")
        return codes

    # ─────────────────────────────────────────
    # 4. 종목별 실시간 등록 (0B/0D)
    # ─────────────────────────────────────────
    async def subscribe_realtime(
        self, items: list[str], types: list[str], grp_no: str = "1"
    ):
        if not items or not types:
            return
        async with self._reg_lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            elapsed = now - self._last_reg_ts
            if elapsed < REG_INTERVAL_SEC:
                await asyncio.sleep(REG_INTERVAL_SEC - elapsed)

            msg = {
                "trnm": "REG", "grp_no": grp_no,
                "refresh": "1",
                "data": [{"item": items, "type": types}],
            }
            await self._send(msg)
            self._last_reg_ts = asyncio.get_event_loop().time()

            for code in items:
                self._subscribed_realtime.setdefault(code, set()).update(types)
            logger.info(f"📡 실시간 등록: {items} types={types}")

    async def unsubscribe_realtime(
        self, items: list[str], types: list[str], grp_no: str = "1"
    ):
        if not items or not types:
            return
        msg = {
            "trnm": "REMOVE", "grp_no": grp_no,
            "data": [{"item": items, "type": types}],
        }
        await self._send(msg)
        for code in items:
            if code in self._subscribed_realtime:
                self._subscribed_realtime[code].difference_update(types)
                if not self._subscribed_realtime[code]:
                    del self._subscribed_realtime[code]
        logger.info(f"📴 실시간 해제: {items} types={types}")

    # ─────────────────────────────────────────
    # 5. 수신 루프
    # ─────────────────────────────────────────
    async def listen(self):
        backoff = 1
        while not self._stop:
            try:
                if not self.connected:
                    # 재연결 판정에 쓸 상태를 connect() 직후 스냅샷으로 고정 —
                    # 재구독 단계(fetch_condition_list/subscribe_*) 중 예외가 나서
                    # 이 블록을 다시 타게 되더라도 원래 단절 시각을 잃지 않게 함.
                    # (2026-07-28: 실전에서 on_reconnect 콜백이 원인 미상으로 한 번
                    # 호출 안 된 사고 발생 — 재현은 못 했으나 방어 차원에서 강화)
                    was_ever_connected = self._ever_connected
                    disconnected_since = self._disconnected_at

                    await self.connect()
                    await self.fetch_condition_list()
                    for seq in list(self._subscribed_seqs):
                        await self.subscribe_condition(seq)
                    for code, types in list(self._subscribed_realtime.items()):
                        await self.subscribe_realtime([code], list(types))
                    backoff = 1

                    logger.info(
                        "🔍 재연결 콜백 판정: ever_connected=%s disconnected_since=%s",
                        was_ever_connected, disconnected_since,
                    )
                    if was_ever_connected and disconnected_since is not None:
                        outage = time.time() - disconnected_since
                        self._disconnected_at = None
                        logger.info("📞 on_reconnect 콜백 호출 (단절 %.1f초)", outage)
                        await self._fire_callback(self.on_reconnect, outage)
                    else:
                        logger.info("⏭️ on_reconnect 콜백 스킵 (최초 연결 또는 단절 기록 없음)")
                    self._ever_connected = True

                while not self._stop:
                    try:
                        raw = await asyncio.wait_for(self.ws.recv(), timeout=IDLE_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.warning(f"⚠️ WS {IDLE_TIMEOUT}s 무수신 → ping 확인")
                        try:
                            pong_waiter = await self.ws.ping()
                            await asyncio.wait_for(pong_waiter, timeout=5)
                            logger.info("✅ WS ping 응답 정상 (연결 살아있음)")
                            continue
                        except Exception:
                            logger.warning("🔌 WS ping 실패 → 좀비 연결 간주, 재연결")
                            break
                    await self._handle_message(raw)

            except websockets.ConnectionClosed as e:
                logger.warning(f"🔌 연결 끊김 (code={e.code}, reason={e.reason})")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"❌ 수신 루프 예외: {e}")

            self.connected = False
            if self._stop:
                break

            if self._disconnected_at is None:
                self._disconnected_at = time.time()
                await self._fire_callback(self.on_disconnect, None)

            jitter = random.uniform(0, backoff * 0.3)
            logger.info(f"♻️ {backoff + jitter:.1f}초 후 재연결 시도...")
            await asyncio.sleep(backoff + jitter)
            backoff = min(backoff * 2, 60)

    async def _fire_callback(self, cb, arg):
        """on_disconnect/on_reconnect 콜백 안전 호출 (async/sync 자동 판별)."""
        if not cb:
            return
        try:
            result = cb() if arg is None else cb(arg)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("WS 상태 콜백 예외")

    async def _handle_message(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"⚠️ JSON 파싱 실패: {raw[:200]}")
            return

        trnm = msg.get("trnm")

        if trnm == "PING":
            await self.ws.send(raw)
            return

        if trnm in ("REAL", "CNSRREQ"):
            await self._dispatch_signal(msg)
            return

        logger.info(f"📥 WS msg trnm={trnm}: {str(msg)[:300]}")

    # ─────────────────────────────────────────
    # 6. 디스패치 (★ async/sync 자동 판별 적용)
    # ─────────────────────────────────────────
    async def _dispatch_signal(self, msg: dict):
        data = msg.get("data")
        if not data:
            # 진단(2026-07-31): 조건검색 실시간 편입 이벤트가 07-31 장중 한 건도
            # 안 잡히는 문제를 조사하려고 추가. trnm=REAL인데 data가 비어있는
            # 경우를 한 번만 기록 — Kiwoom이 조건편입을 REAL로 안 보내거나
            # 다른 payload 형태로 보낼 가능성을 확인하기 위함.
            if msg.get("trnm") == "REAL" and not self._real_empty_logged:
                self._real_empty_logged = True
                logger.info(f"🔎 REAL 메시지인데 data 비어있음(원본): {str(msg)[:400]}")
            return
        if isinstance(data, dict):
            data = [data]

        for item in data:
            item_type = item.get("type")
            # 진단(2026-07-31): 0B/0D/0g 외에 처음 보는 item_type이 오면 1회 기록.
            # 조건검색 실시간 편입이 우리가 모르는 type 코드로 오고 있어서
            # _dispatch_condition_item으로 잘못 안 가고 있을 가능성을 확인.
            if item_type not in self._seen_item_types:
                self._seen_item_types.add(item_type)
                logger.info(f"🔎 신규 item_type='{item_type}' 최초 관측: {str(item)[:400]}")
            if item_type == TYPE_TRADE:
                await self._dispatch_trade_item(item)
            elif item_type == TYPE_ORDERBOOK:
                await self._dispatch_orderbook_item(item)
            elif item_type == TYPE_PROGRAM:
                await self._dispatch_program_item(item)
            else:
                await self._dispatch_condition_item(item)

    async def _dispatch_trade_item(self, item: dict):
        if not self.on_trade:
            return

        stock_code = (item.get("item") or "").lstrip("A").strip()
        values = item.get("values") or {}

        # 0B(주식체결) 실시간에 어떤 FID가 실려오는지 하루 1회만 기록 (2026-07-31).
        # 0D(호가)는 예전부터 남기고 있었는데 0B는 없어서, 프로그램매매 관련
        # 데이터가 WS로 공짜로 들어오는지 확인할 방법이 없었다. REST는 이미
        # 429가 하루 2천 건 넘게 나는 포화 상태라(07-30 기준 ka10080만 2,223건),
        # 프로그램 순매수를 REST 종목별 폴링으로 가져오는 건 사실상 불가능 —
        # WS에 이미 실려 있다면 추가 호출 0으로 해결된다.
        if not getattr(self, "_trade_keys_logged", False):
            self._trade_keys_logged = True
            logger.info(f"🔑 0B 체결 raw 키: {sorted(values.keys(), key=str)}")

        price = self._parse_signed_int(values.get("10"))
        volume_signed = self._parse_signed_int(values.get("15"))

        if volume_signed > 0:
            side = "buy"
        elif volume_signed < 0:
            side = "sell"
        else:
            side = "neutral"

        parsed = {
            "stock_code": stock_code,
            "price": abs(price),
            "volume": abs(volume_signed),
            "side": side,
            "strength": self._parse_float(values.get("228")),
            "time": values.get("20", ""),
            "raw": values,
        }
        try:
            # ★ 콜백이 async면 바로 스케줄링, sync면 백그라운드 스레드로 실행
            if asyncio.iscoroutinefunction(self.on_trade):
                asyncio.create_task(self.on_trade(parsed))
            else:
                asyncio.create_task(asyncio.to_thread(self.on_trade, parsed))
        except Exception:
            logger.exception(f"on_trade 콜백 예외: {stock_code}")

    async def _dispatch_orderbook_item(self, item: dict):
        if not self.on_orderbook:
            return
        stock_code = (item.get("item") or "").lstrip("A").strip()
        values = item.get("values") or {}

        if not getattr(self, "_orderbook_keys_logged", False):
            logger.info(f"🔑 0D 호가 raw 키: {list(values.keys())}")
            self._orderbook_keys_logged = True

        ask_prices, ask_volumes, bid_prices, bid_volumes = [], [], [], []
        for i in range(1, 11):
            ap = values.get(str(40 + i))
            av = values.get(str(60 + i))
            bp = values.get(str(50 + i))
            bv = values.get(str(70 + i))
            if ap: ask_prices.append(self._parse_uint(ap))
            if av: ask_volumes.append(self._parse_uint(av))
            if bp: bid_prices.append(self._parse_uint(bp))
            if bv: bid_volumes.append(self._parse_uint(bv))

        parsed = {
            "stock_code": stock_code,
            "ask_prices": ask_prices,
            "ask_volumes": ask_volumes,
            "bid_prices": bid_prices,
            "bid_volumes": bid_volumes,
            "raw": values,
        }
        try:
            # ★ 콜백이 async면 바로 스케줄링, sync면 백그라운드 스레드로 실행
            if asyncio.iscoroutinefunction(self.on_orderbook):
                asyncio.create_task(self.on_orderbook(parsed))
            else:
                asyncio.create_task(asyncio.to_thread(self.on_orderbook, parsed))
        except Exception:
            logger.exception(f"on_orderbook 콜백 예외: {stock_code}")

    async def _dispatch_program_item(self, item: dict):
        """종목프로그램매매(0g) 파싱 (2026-07-31 신규).

        FID 118/119/120/122/123/124는 장 시작부터의 **누적값**(다른 누적계열
        FID들, 예: 13 누적거래량과 동일 관례)이라고 보고 그대로 전달한다 —
        분 단위 순유입(델타)으로의 변환은 여기서 하지 않고
        core/program_flow.ProgramFlowTracker.record_cumulative()에 위임한다
        (그쪽이 종목별 마지막 누적값을 기억하고 있어야 델타를 낼 수 있어서
        parsing 계층인 여기 둘 이유가 없음).

        raw 키 로깅은 최초 1회만 — 여기 실려있는 실제 FID 구성을 확인해 이
        타입 코드("0g")가 맞는지, FID 번호가 문서와 일치하는지 검증하는 용도.
        0B/0D는 찍히는데 이게 안 찍히면 타입 코드부터 재확인할 것."""
        if not self.on_program:
            return
        stock_code = (item.get("item") or "").lstrip("A").strip()
        values = item.get("values") or {}

        if not self._program_keys_logged:
            logger.info(f"🔑 0g 프로그램매매 raw 키: {sorted(values.keys(), key=str)}")
            self._program_keys_logged = True

        parsed = {
            "stock_code": stock_code,
            "sell_qty_cum": self._parse_signed_int(values.get("118")),
            "buy_qty_cum": self._parse_signed_int(values.get("119")),
            "net_qty_cum": self._parse_signed_int(values.get("120")),
            "sell_amt_cum": self._parse_signed_int(values.get("122")),
            "buy_amt_cum": self._parse_signed_int(values.get("123")),
            "net_amt_cum": self._parse_signed_int(values.get("124")),
            "time": values.get("20", ""),
            "raw": values,
        }
        try:
            if asyncio.iscoroutinefunction(self.on_program):
                asyncio.create_task(self.on_program(parsed))
            else:
                asyncio.create_task(asyncio.to_thread(self.on_program, parsed))
        except Exception:
            logger.exception(f"on_program 콜백 예외: {stock_code}")

    async def _dispatch_condition_item(self, item: dict):
        stock_code = (
            item.get("9001") or item.get("jmcode") or item.get("stk_cd") or ""
        ).strip()
        if stock_code.startswith("A"):
            stock_code = stock_code[1:]
        signal_type = item.get("843") or item.get("insert_delete_tp") or "I"
        cond_seq = str(
            item.get("841") or item.get("cond_idx") or item.get("seq") or ""
        ).strip()
        if not stock_code:
            return

        if not self._cond_keys_logged:
            logger.info(f"🔑 조건 실시간 raw 키: {list(item.keys())}")
            self._cond_keys_logged = True

        mark = "📈" if signal_type == "I" else "📉"
        kind = "편입" if signal_type == "I" else "이탈"
        logger.info(f"{mark} {kind} 신호: {stock_code} (seq={cond_seq or '?'})")
        if self.on_signal:
            try:
                # ★ 콜백이 async면 바로 스케줄링, sync면 백그라운드 스레드로 실행
                if asyncio.iscoroutinefunction(self.on_signal):
                    asyncio.create_task(
                        self.on_signal(stock_code, signal_type, item, cond_seq or None)
                    )
                else:
                    asyncio.create_task(asyncio.to_thread(
                        self.on_signal, stock_code, signal_type, item, cond_seq or None
                    ))
            except Exception:
                logger.exception(f"on_signal 콜백 예외: {stock_code}")

    # ─────────────────────────────────────────
    # 7. 파싱 유틸
    # ─────────────────────────────────────────
    @staticmethod
    def _parse_signed_int(s) -> int:
        if not s:
            return 0
        try:
            return int(str(s).strip())
        except ValueError:
            return 0

    @staticmethod
    def _parse_uint(s) -> int:
        if not s:
            return 0
        try:
            return abs(int(str(s).strip()))
        except ValueError:
            return 0

    @staticmethod
    def _parse_float(s) -> float:
        if not s:
            return 0.0
        try:
            return float(str(s).strip())
        except ValueError:
            return 0.0

    # ─────────────────────────────────────────
    # 8. 송신/대기 유틸
    # ─────────────────────────────────────────
    async def _send(self, payload: dict):
        if not self.ws:
            raise RuntimeError("WebSocket 미연결 상태")
        await self.ws.send(json.dumps(payload))

    async def _wait_for(self, trnm: str, seq: str | None = None, timeout: int = 10) -> dict:
        """trnm(+seq)이 일치하는 응답이 올 때까지 직접 recv().

        (2026-07-31) seq 필터 추가 — CNSRREQ는 조건식 실시간 등록(search_type=1)
        요청과 스냅샷(search_type=0) 요청이 전부 같은 trnm을 쓰기 때문에, seq
        없이 "trnm만" 보고 첫 매치를 반환하면 다른 조건의 응답을 엉뚱하게
        가로챌 수 있다. 실제로 07-31 기동 로그에서 seq=1 스냅샷을 요청했는데
        본문에 'seq':'3'이 찍힌 응답을 받아오는 교차매칭이 확인됐다(주도주상위
        요청 -> 눌림목자동 응답, 식으로 한 칸씩 밀림). seq가 안 맞는 CNSRREQ는
        버리지 않고 다시 대기 목록으로 넘겨(등록 ack 등 다른 용도로 온 것일 수
        있으니) 계속 기다린다. seq=None이면 기존처럼 trnm만 확인(CNSRLST 등
        seq 개념이 없는 응답용)."""
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            remaining = end - loop.time()
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise RuntimeError(f"{trnm} 응답 타임아웃 ({timeout}s)")
            msg = json.loads(raw)
            if msg.get("trnm") == "PING":
                await self.ws.send(raw)
                continue
            if msg.get("trnm") == trnm:
                if seq is None or str(msg.get("seq")) == str(seq):
                    return msg
                # seq 불일치 — 다른 요청(등록 ack 등)에 대한 응답이므로 버리지
                # 않고 로그만 남기고 계속 대기(내가 기다리는 응답은 아직 온 게
                # 아님). 이 메시지 자체는 실시간 틱/조건편입이 아니라 CNSRREQ류
                # 부수 응답이라 유실돼도 매매 판단에 영향 없음.
                logger.info(f"↪️ {trnm} seq 불일치(요청={seq}, 응답={msg.get('seq')}) — 계속 대기")
        raise RuntimeError(f"{trnm} 응답 타임아웃 (seq={seq})")

    async def close(self):
        self._stop = True
        if self.ws:
            try:
                for seq in list(self._subscribed_seqs):
                    await self.unsubscribe_condition(seq)
                for code, types in list(self._subscribed_realtime.items()):
                    await self.unsubscribe_realtime([code], list(types))
            except Exception:
                pass
            await self.ws.close()
        logger.info("👋 WebSocket 종료")
