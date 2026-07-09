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
import websockets

from config import settings
from utils.logger import logger


TYPE_TRADE = "0B"
TYPE_ORDERBOOK = "0D"

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
        is_mock: bool = True,
        on_signal=None,
        on_trade=None,
        on_orderbook=None,
    ):
        self.token = token
        self.url = self.MOCK_URL if is_mock else self.REAL_URL
        self.on_signal = on_signal
        self.on_trade = on_trade
        self.on_orderbook = on_orderbook

        self.ws = None
        self.connected = False
        self._stop = False

        self._subscribed_seqs: list[str] = []
        self.condition_map: dict[str, str] = {}
        self._subscribed_realtime: dict[str, set[str]] = {}
        self._cond_keys_logged = False
        self._orderbook_keys_logged = False

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
        self, seq: str, stex_tp: str = "K", timeout: int = 10,
    ) -> list[str]:
        seq = str(seq)
        await self._send({
            "trnm": "CNSRREQ", "seq": seq, "search_type": "0",
            "stex_tp": stex_tp, "cont_yn": "N", "next_key": "",
        })

        try:
            resp = await self._wait_for("CNSRREQ", timeout=timeout)
        except RuntimeError as e:
            logger.warning(f"⚠️ 조건식 스냅샷 [seq={seq}] 응답 없음: {e}")
            return []

        logger.info(f"🔍 CNSRREQ 응답원본 [seq={seq}]: {str(resp)[:500]}")

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
                    await self.connect()
                    await self.fetch_condition_list()
                    for seq in list(self._subscribed_seqs):
                        await self.subscribe_condition(seq)
                    for code, types in list(self._subscribed_realtime.items()):
                        await self.subscribe_realtime([code], list(types))
                    backoff = 1

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

            logger.info(f"♻️ {backoff}초 후 재연결 시도...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

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
            return
        if isinstance(data, dict):
            data = [data]

        for item in data:
            item_type = item.get("type")
            if item_type == TYPE_TRADE:
                await self._dispatch_trade_item(item)
            elif item_type == TYPE_ORDERBOOK:
                await self._dispatch_orderbook_item(item)
            else:
                await self._dispatch_condition_item(item)

    async def _dispatch_trade_item(self, item: dict):
        if not self.on_trade:
            return

        stock_code = (item.get("item") or "").lstrip("A").strip()
        values = item.get("values") or {}

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

    async def _wait_for(self, trnm: str, timeout: int = 10) -> dict:
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
                return msg
        raise RuntimeError(f"{trnm} 응답 타임아웃")

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