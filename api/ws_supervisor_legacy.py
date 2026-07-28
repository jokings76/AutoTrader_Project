# ws_supervisor.py
"""
WebSocket 재연결 슈퍼바이저 (WSSupervisor)
==========================================
목적: WS 끊김 대응 — (a)자동 재연결+백오프, (b)half-open(에러 없이 데이터만 끊김) 감지,
      (c)재연결마다 조건검색 재등록 강제.

★ 가장 중요 ★
  키움 WS는 끊겼다 다시 붙으면 조건검색[0150] 등록(CNSRREQ seq=6/7/8)이 날아간다.
  재연결돼도 재등록을 안 하면 연결은 멀쩡해 보여도 통지가 0건이 된다.
  → 조건등록 코드는 run_once_fn '안', 연결+로그인 직후에 '매번' 넣을 것.
  (증상 '조건검색과 코딩 불일치 / 끊김 / 스냅샷 0종목'의 유력한 한 원인.)

라이브러리 비의존
  websocket-client(WebSocketApp)든 websockets(asyncio)든 상관없이,
  '연결 + 로그인 + 조건등록 + 수신루프(블로킹)' 한 덩어리를 run_once_fn으로 감싸면 됨.

half-open 감지 (반쯤 죽은 소켓)
  - 수신 콜백에서 매 메시지마다 sup.mark_alive() 호출(키움 PINGPONG echo 지점이면 충분).
  - heartbeat_timeout 동안 한 건도 안 오면 죽은 연결로 보고 강제 재연결.
  - ※ 블로킹 ws.recv()는 stop_event만으론 안 깨진다. 둘 중 하나 필요:
      (1) 수신 소켓에 타임아웃 설정(ws.settimeout(5)) → recv가 주기적으로 풀려 stop_event 확인
      (2) on_force_close 콜백 제공 → 워치독이 ws.close()로 강제로 깨움
    가능하면 (1)+(2) 둘 다 권장.

연결 방법
  1) 기존 WS 코드를 함수 하나로 묶기 (stop_event 인자 받게):
       def _run_ws_once(stop_event):
           ws = connect()                 # 연결
           ws.settimeout(5)               # ★ 권장: recv가 주기적으로 풀리게
           login(ws)                      # 로그인
           register_conditions(ws)        # ★★ seq=6/7/8 조건검색 재등록 (매 연결마다!)
           while not stop_event.is_set():
               try:
                   msg = ws.recv()        # 블로킹(타임아웃 5s)
               except TimeoutError:
                   continue               # 타임아웃은 정상 — 루프 재확인
               sup.mark_alive()           # half-open 감지용
               handle(msg)                # PINGPONG echo / REAL / 조건통지 처리
           ws.close()

  2) 슈퍼바이저 (봇 기동 시):
       from ws_supervisor import WSSupervisor
       sup = WSSupervisor(
           _run_ws_once,
           heartbeat_timeout=30,          # 30s 무수신 → 강제 재연결 (0이면 끔)
           on_force_close=lambda: ws_ref and ws_ref.close(),  # 선택: 블로킹 recv 깨우기
           logger=log,
       )
       sup.start()

  3) 종료: sup.stop()
"""

from __future__ import annotations

import time
import random
import threading
from typing import Any, Callable, Optional


class WSSupervisor:
    def __init__(
        self,
        run_once_fn: Callable[[threading.Event], Any],
        on_state: Optional[Callable[[str], None]] = None,
        on_force_close: Optional[Callable[[], None]] = None,
        base_backoff: float = 1.0,
        max_backoff: float = 30.0,
        healthy_reset_sec: float = 60.0,
        heartbeat_timeout: float = 0.0,   # 0이면 half-open 감지 끔
        logger: Any = None,
    ) -> None:
        """
        run_once_fn(stop_event): 연결+로그인+조건등록+수신루프(블로킹). 끊기면 리턴/예외 → 재연결.
        on_state(state)        : 'connecting'/'connected'/'disconnected'/'reconnecting' 콜백(선택).
        on_force_close()       : 워치독/종료 시 블로킹 recv를 깨우기 위한 강제 close(선택, 권장).
        base/max_backoff       : 재연결 대기 1s→2s→4s...→30s(jitter 포함).
        healthy_reset_sec      : 이 시간 이상 붙어있다 끊기면 백오프를 base로 리셋.
        heartbeat_timeout      : 무수신 이 시간 초과 시 half-open으로 보고 강제 재연결.
        """
        self._run_once = run_once_fn
        self._on_state = on_state
        self._on_force_close = on_force_close
        self._base = float(base_backoff)
        self._max = float(max_backoff)
        self._healthy = float(healthy_reset_sec)
        self._hb_timeout = float(heartbeat_timeout)
        self._log = logger

        self._running = False
        self._sup_thread: Optional[threading.Thread] = None
        self._session_stop: Optional[threading.Event] = None
        self._last_alive = 0.0
        self._lock = threading.RLock()
        self._reconnects = 0

    # ---- lifecycle ----
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._sup_thread = threading.Thread(target=self._supervise, name="ws_supervisor", daemon=True)
        self._sup_thread.start()
        self._info("🧵 ws_supervisor 시작")

    def stop(self, timeout: float = 5.0) -> None:
        if not self._running:
            return
        self._running = False
        if self._session_stop:
            self._session_stop.set()
        self._force_close()   # 블로킹 recv 깨우기
        if self._sup_thread:
            self._sup_thread.join(timeout=timeout)
        self._info(f"🧵 ws_supervisor 종료 (재연결 {self._reconnects}회)")

    def mark_alive(self) -> None:
        """수신 콜백에서 매 메시지마다 호출. half-open 감지의 기준점."""
        with self._lock:
            self._last_alive = time.time()

    def reconnect_count(self) -> int:
        return self._reconnects

    # ---- core loop ----
    def _supervise(self) -> None:
        backoff = self._base
        first = True
        while self._running:
            self._session_stop = threading.Event()
            self.mark_alive()
            self._emit_state("connecting" if first else "reconnecting")
            first = False
            t0 = time.time()

            wd = None
            if self._hb_timeout > 0:
                wd = threading.Thread(
                    target=self._watchdog, args=(self._session_stop,),
                    name="ws_heartbeat", daemon=True,
                )
                wd.start()

            try:
                self._emit_state("connected")
                self._run_once(self._session_stop)   # 블로킹: 끊기면 리턴/예외
            except Exception as e:  # noqa: BLE001
                self._err(f"🔴 WS 세션 예외: {e}")
            finally:
                self._session_stop.set()
                if wd:
                    wd.join(timeout=2.0)

            if not self._running:
                break

            self._reconnects += 1
            self._emit_state("disconnected")
            alive = time.time() - t0
            if alive >= self._healthy:
                backoff = self._base   # 오래 붙어있었으면 백오프 리셋
            self._warn(f"🔌 WS 끊김 (유지 {alive:.0f}s) → {backoff:.1f}s 후 재연결 (#{self._reconnects})")
            self._sleep_backoff(backoff)
            backoff = min(backoff * 2, self._max)

    def _watchdog(self, session_stop: threading.Event) -> None:
        while not session_stop.is_set() and self._running:
            time.sleep(1.0)
            with self._lock:
                silent = time.time() - self._last_alive
            if silent > self._hb_timeout:
                self._warn(f"💤 WS {silent:.0f}s 무수신(half-open 의심) → 강제 재연결")
                session_stop.set()
                self._force_close()
                return

    # ---- helpers ----
    def _sleep_backoff(self, backoff: float) -> None:
        delay = backoff + random.uniform(0, backoff * 0.3)   # jitter
        end = time.time() + delay
        while time.time() < end and self._running:
            time.sleep(0.2)

    def _force_close(self) -> None:
        if self._on_force_close:
            try:
                self._on_force_close()
            except Exception as e:  # noqa: BLE001
                self._err(f"on_force_close 예외: {e}")

    def _emit_state(self, state: str) -> None:
        if self._on_state:
            try:
                self._on_state(state)
            except Exception as e:  # noqa: BLE001
                self._err(f"on_state 콜백 예외: {e}")

    def _info(self, m: str):
        if self._log: self._log.info(m)
    def _warn(self, m: str):
        if self._log: self._log.warning(m)
    def _err(self, m: str):
        if self._log: self._log.error(m)