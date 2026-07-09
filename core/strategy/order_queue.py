# order_queue.py
"""
주문 큐 + 주문 워커 (OrderQueue)
===============================================
목적: strategy 루프에서 '주문 실행'을 떼어내 시간차/끊김 제거.
  - 지금: strategy_manager가 BUY 확정 직후 주문 함수를 '동기'로 호출 → 네트워크 타는 동안 루프 정지
  - 변경: strategy_manager는 signal dict를 큐에 put()만(논블로킹). 별도 워커 스레드가 꺼내서 실제 주문.

설계 원칙
  - threading 기반(네 봇의 기존 스레드 모델과 동일, asyncio 전면 재작성 X).
  - 의존성 주입: 실제 주문은 place_order_fn 콜러블로 받음 → OrderManager 시그니처를 안 건드림.
  - 지정가 +1틱(시장가 금지, 슬리피지 최소화). KRX 호가가격단위 표 내장(2023-01-25 개정, 7단계).
  - 종목당 in-flight 1건 dedup → 빠른 tick 연사로 인한 중복 주문 방지.
    (※ 슬롯/MAX_HOLDINGS=5 게이팅을 '대체'하는 게 아니라, 접수~확정 사이 중복발사만 막는 보호막)
  - 재시도는 '거래소에 도달 못 한 게 명확할 때'만. 애매하면 재시도 금지(중복매수 위험).
  - 결과는 on_result 콜백으로 strategy에 돌려줌 → holdings/positions 갱신은 기존 로직 유지.

KRX 호가가격단위 (2023-01-25 개정, 코스피/코스닥/코넥스 통일, 7단계)
  <2,000:1 / 2,000~5,000:5 / 5,000~20,000:10 / 20,000~50,000:50 /
  50,000~200,000:100 / 200,000~500,000:500 / >=500,000:1,000
  ※ WebSocket 호가(매도호가1)가 있으면 그 값을 price로 넘기는 게 표 계산보다 정확.

연결 방법
  1) 생성 (봇 기동 시 1회):
       from order_queue import OrderQueue
       self.order_q = OrderQueue(
           place_order_fn=self.order_manager.buy,   # 너의 실제 매수 함수(콜러블)
           on_result=self._on_order_result,         # 결과 콜백(아래 2)
           max_retries=2, logger=log,
       )
       self.order_q.start()
  2) 결과 콜백 (holdings 갱신 등 기존 로직 연결):
       def _on_order_result(self, signal, result):
           if isinstance(result, dict) and result.get("ok"):
               ...  # 접수/체결 처리 (holdings 등록 등)
           else:
               ...  # 실패 처리 (로그/슬롯 복구 등)
  3) strategy_manager의 'BUY 확정' 지점 교체:
       # 기존(동기 호출 제거): self.order_manager.buy(code, qty, price)
       self.order_q.put({"stock_code": code, "qty": qty, "price": cur_price, "phase": "1A"})
  4) 종료 시:
       self.order_q.stop()

place_order_fn 시그니처가 buy(code, qty, price)가 아니면 람다 어댑터만:
   place_order_fn=lambda code, qty, price: self.order_manager.send_order(
       code=code, ord_qty=qty, ord_prc=price, ord_type="buy")

반환값 권장: dict {"ok": bool, "retryable": bool(optional), ...}.
  - bool/주문번호(str) 반환이어도 동작(truthy=성공)하지만, 재시도 정밀도를 위해 dict 권장.
"""

from __future__ import annotations

import time
import queue
import threading
from typing import Any, Callable, Dict, Optional


# ===================== KRX 호가가격단위 (2023-01-25 개정) ===================== #
_KRX_TICK_TABLE = (
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
    (float("inf"), 1_000),
)


def krx_tick_size(price: float) -> int:
    for upper, tick in _KRX_TICK_TABLE:
        if price < upper:
            return tick
    return 1_000


def buy_limit_price(price: float, ticks: int = 1) -> int:
    """기준가를 틱 그리드에 스냅(내림)한 뒤 +ticks 호가. 지정가 매수용."""
    tick = krx_tick_size(price)
    base = int(price // tick) * tick
    return base + tick * ticks


# ===================== 주문 큐 ===================== #
class OrderQueue:
    _SENTINEL = object()

    def __init__(
        self,
        place_order_fn: Callable[..., Any],
        on_result: Optional[Callable[[dict, Any], None]] = None,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
        limit_ticks: int = 1,
        dedup: bool = True,
        logger: Any = None,
    ) -> None:
        self._place = place_order_fn
        self._on_result = on_result
        self._max_retries = int(max_retries)
        self._backoff = float(retry_backoff)
        self._limit_ticks = int(limit_ticks)
        self._dedup = dedup
        self._log = logger

        self._q: "queue.Queue" = queue.Queue()
        self._inflight: set = set()
        self._lock = threading.RLock()
        self._worker: Optional[threading.Thread] = None
        self._running = False
        # stats
        self._put = 0
        self._skipped = 0
        self._ok = 0
        self._fail = 0
        self._retry = 0

    # ---- lifecycle ----
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._run, name="order_worker", daemon=True)
        self._worker.start()
        self._info("🧵 order_worker 시작")

    def stop(self, drain: bool = False, timeout: float = 5.0) -> None:
        if not self._running:
            return
        self._running = False
        if not drain:  # 미처리 신호 폐기 (drain=True면 남은 것 처리 후 종료)
            try:
                while True:
                    self._q.get_nowait()
                    self._q.task_done()
            except queue.Empty:
                pass
        self._q.put(self._SENTINEL)
        if self._worker:
            self._worker.join(timeout=timeout)
        self._info(
            f"🧵 order_worker 종료 (put={self._put} ok={self._ok} "
            f"fail={self._fail} skip={self._skipped} retry={self._retry})"
        )

    # ---- enqueue ----
    def put(self, signal: dict) -> bool:
        """strategy_manager가 BUY 확정 시 호출. 즉시 반환(논블로킹). dedup 시 중복 종목은 skip."""
        code = signal.get("stock_code")
        if not code:
            self._warn(f"⚠️ stock_code 없는 신호 무시: {signal}")
            return False
        if self._dedup:
            with self._lock:
                if code in self._inflight:
                    self._skipped += 1
                    self._dbg(f"⏭️ in-flight 중복 [{code}] 신호 skip")
                    return False
                self._inflight.add(code)
        self._put += 1
        self._q.put(signal)
        return True

    # ---- worker ----
    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is self._SENTINEL:
                self._q.task_done()
                break
            try:
                self._process(item)
            except Exception as e:  # noqa: BLE001
                self._fail += 1
                self._err(f"🔴 order_worker 처리 예외 [{item.get('stock_code')}]: {e}")
                self._emit_result(item, {"ok": False, "error": str(e)})
            finally:
                self._release(item.get("stock_code"))
                self._q.task_done()

    def _process(self, signal: dict) -> None:
        code = signal["stock_code"]
        qty = signal.get("qty")
        price = signal.get("price")
        phase = signal.get("phase", "")

        if not qty or not price:
            self._warn(f"⚠️ qty/price 누락 [{code}] {signal}")
            self._emit_result(signal, {"ok": False, "error": "missing qty/price"})
            return

        limit = buy_limit_price(price, self._limit_ticks)

        attempt = 0
        while True:
            attempt += 1
            try:
                result = self._place(code, qty, limit)
            except Exception as e:  # 예외 = 거래소 도달 실패 가능성↑ → 재시도 대상
                if attempt <= self._max_retries:
                    self._retry += 1
                    self._warn(f"🔁 주문 예외 재시도 {attempt}/{self._max_retries} [{code}]: {e}")
                    time.sleep(self._backoff * attempt)
                    continue
                self._fail += 1
                self._err(f"🔴 주문 최종 실패(예외) [{code}]: {e}")
                self._emit_result(signal, {"ok": False, "error": str(e)})
                return

            ok, retryable = self._interpret(result)
            if ok:
                self._ok += 1
                self._info(f"🟢 BUY[{phase}] 접수 [{code}] x{qty} @지정가 {limit:,} (시도 {attempt})")
                self._emit_result(signal, result)
                return
            if retryable and attempt <= self._max_retries:
                self._retry += 1
                self._warn(f"🔁 주문 거부 재시도 {attempt}/{self._max_retries} [{code}] result={result}")
                time.sleep(self._backoff * attempt)
                continue
            self._fail += 1
            self._err(f"🔴 주문 실패 [{code}] result={result}")
            self._emit_result(signal, result)
            return

    # ---- helpers ----
    @staticmethod
    def _interpret(result: Any):
        """주문 결과 해석 → (성공여부, 재시도가능). 애매하면 재시도 금지(중복매수 방지)."""
        if isinstance(result, dict):
            ok = bool(result.get("ok", result.get("success", False)))
            return ok, bool(result.get("retryable", False))
        if isinstance(result, bool):
            return result, False
        # None/주문번호(str 등): truthy면 성공 간주, 재시도는 안 함
        return bool(result), False

    def _release(self, code: Optional[str]) -> None:
        if self._dedup and code:
            with self._lock:
                self._inflight.discard(code)

    def _emit_result(self, signal: dict, result: Any) -> None:
        if self._on_result:
            try:
                self._on_result(signal, result)
            except Exception as e:  # noqa: BLE001
                self._err(f"🔴 on_result 콜백 예외 [{signal.get('stock_code')}]: {e}")

    def pending(self) -> Dict[str, Any]:
        with self._lock:
            return {"queued": self._q.qsize(), "inflight": list(self._inflight)}

    def stats(self) -> Dict[str, int]:
        return {
            "put": self._put, "ok": self._ok, "fail": self._fail,
            "skipped": self._skipped, "retry": self._retry,
            "inflight": len(self._inflight),
        }

    # ---- 로깅 (logger 없으면 무음) ----
    def _info(self, m: str):
        if self._log: self._log.info(m)
    def _warn(self, m: str):
        if self._log: self._log.warning(m)
    def _err(self, m: str):
        if self._log: self._log.error(m)
    def _dbg(self, m: str):
        if self._log: self._log.debug(m)