# api/candle_cache.py
"""
분봉 캐시 레이어 (CandleCache)
================================
목적: ka10080 분봉 호출 폭주 → 429 → 2초 blocking 재시도 → strategy 루프 정지(끊김)/시간차 완화

동작 원리
  1) 완성된 분봉은 같은 분(minute) 안에서 변하지 않는다.
     → TTL(기본 8초) 동안은 같은 종목 재요청을 캐시로 응답하여 ka10080 호출 자체를 줄임.
  2) 호출 실패(429/타임아웃/예외) 시 마지막 정상 캐시(stale)를 그대로 반환.
     → 네트워크가 흔들려도 루프가 멈추지 않음(끊김 방지). 핫패스의 2초 blocking 재시도 제거.
  3) hit / miss / stale / error 통계를 남겨, 풀타임 dry-run 후 실제 호출 절감량을 확인.

주의
  - 현재 형성 중인 봉([0], newest-first)은 분 안에서 close/high/low/volume이 변한다.
    60MA 등은 '완성봉' 기준이므로 8초 staleness는 무해. 현재가는 캐시 말고 WebSocket tick을 사용.
  - 락은 캐시 조회/기록만 보호하고, 실제 네트워크 호출은 락 밖에서 수행(다른 종목 블로킹 방지).
    동일 종목 동시 miss 시 중복 호출이 날 수 있으나 10~30종목 규모에선 무시 가능.

연결 방법 (예: api/kiwoom_rest.py)
    from api.candle_cache import CandleCache

    # 기존 원본 호출을 _raw 로 남겨두고:
    #   def _raw_get_minute_candles(self, stock_code, ...): ... (지금의 get_minute_candles 본문)
    self._candle_cache = CandleCache(self._raw_get_minute_candles, ttl_sec=8.0, logger=log)

    # 호출부 교체:
    def get_minute_candles(self, stock_code, *args, **kwargs):
        return self._candle_cache.get(stock_code, *args, **kwargs)

    # MA용 '최신 형성봉'이 꼭 필요한 극히 일부 지점만:
    candles = self._candle_cache.get(stock_code, force=True)

    # Phase 전환 로그(⏰)나 종료 시점에 1회:
    self._candle_cache.log_stats()
"""

from __future__ import annotations

import time
import threading
from typing import Any, Callable, Dict, List, Optional


class CandleCache:
    def __init__(
        self,
        fetch_fn: Callable[..., List[Dict[str, Any]]],
        ttl_sec: float = 8.0,
        stale_on_error: bool = True,
        logger: Any = None,
    ) -> None:
        """
        fetch_fn       : 실제 분봉을 가져오는 콜러블. (stock_code, *args, **kwargs) -> list[dict]
                         즉 지금의 get_minute_candles 본문(_raw)을 그대로 넣으면 됨.
        ttl_sec        : 캐시 유효시간(초). 8초면 완성봉 기준 무해하면서 ka10080 호출을 크게 절감.
        stale_on_error : True면 호출 실패/빈응답 시 마지막 정상 캐시를 반환(끊김 방지).
        logger         : 기존 'AutoTrader'/root 로거. None이면 무음.
        """
        self._fetch_fn = fetch_fn
        self._ttl = float(ttl_sec)
        self._stale_on_error = stale_on_error
        self._log = logger
        self._lock = threading.RLock()
        # key -> {"data": list, "ts": float}
        self._store: Dict[str, Dict[str, Any]] = {}
        self._hit = 0
        self._miss = 0
        self._stale = 0
        self._error = 0

    # ------------------------------------------------------------------ #
    @staticmethod
    def _key(stock_code: str, args: tuple, kwargs: dict) -> str:
        if not args and not kwargs:
            return stock_code
        return f"{stock_code}|{args}|{tuple(sorted(kwargs.items()))}"

    def get(self, stock_code: str, *args, force: bool = False, **kwargs):
        """
        캐시가 신선하면 캐시 반환, 아니면 실제 호출.
        실패 시 stale_on_error에 따라 직전 캐시 반환(없으면 None).
        반환 형식은 fetch_fn과 동일 (candle dict 리스트, newest-first).
        """
        key = self._key(stock_code, args, kwargs)
        now = time.time()

        with self._lock:
            entry = self._store.get(key)
            if (not force) and entry and (now - entry["ts"] < self._ttl):
                self._hit += 1
                return entry["data"]

        # ---- 캐시 미스: 실제 호출 (락 밖에서 수행) ----
        try:
            data = self._fetch_fn(stock_code, *args, **kwargs)
        except Exception as e:  # noqa: BLE001  (429 포함 모든 호출 실패)
            self._error += 1
            with self._lock:
                entry = self._store.get(key)
            if self._stale_on_error and entry:
                self._stale += 1
                self._dbg(
                    f"📊 분봉 호출 실패({e}) → stale 캐시 반환 [{stock_code}] "
                    f"(age={now - entry['ts']:.1f}s)"
                )
                return entry["data"]
            self._warn(f"📊 분봉 호출 실패 & 캐시없음 [{stock_code}]: {e}")
            return None

        if data:
            with self._lock:
                self._store[key] = {"data": data, "ts": time.time()}
                self._miss += 1
            return data

        # 빈 응답([] / None): 가능하면 직전 캐시 유지(일시적 빈응답으로 인한 '분봉부족' 오판 방지)
        with self._lock:
            entry = self._store.get(key)
        if self._stale_on_error and entry:
            self._stale += 1
            return entry["data"]
        self._miss += 1
        return data  # [] 또는 None 그대로 → 호출부의 '분봉부족' 처리 흐름 유지

    # ------------------------------------------------------------------ #
    def invalidate(self, stock_code: Optional[str] = None) -> None:
        """특정 종목(또는 전체) 캐시 무효화. 청산 직후 재매수 판단 등에서 강제 갱신용."""
        with self._lock:
            if stock_code is None:
                self._store.clear()
                return
            for k in [
                k for k in self._store
                if k == stock_code or k.startswith(stock_code + "|")
            ]:
                self._store.pop(k, None)

    def stats(self) -> Dict[str, Any]:
        total = self._hit + self._miss
        rate = (self._hit / total * 100.0) if total else 0.0
        return {
            "hit": self._hit,
            "miss": self._miss,
            "stale": self._stale,
            "error": self._error,
            "hit_rate_pct": round(rate, 1),
            "cached_codes": len(self._store),
        }

    def log_stats(self) -> None:
        s = self.stats()
        self._info(
            f"📊 분봉캐시 통계 hit={s['hit']} miss={s['miss']} "
            f"stale={s['stale']} err={s['error']} "
            f"hit율={s['hit_rate_pct']}% 종목수={s['cached_codes']}"
        )

    # ---- 로깅 헬퍼 (logger 없으면 무음) ----
    def _info(self, msg: str) -> None:
        if self._log:
            self._log.info(msg)

    def _warn(self, msg: str) -> None:
        if self._log:
            self._log.warning(msg)

    def _dbg(self, msg: str) -> None:
        if self._log:
            self._log.debug(msg)