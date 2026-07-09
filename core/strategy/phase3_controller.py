"""
Phase 3 컨트롤러 — 체결강도 기반 진입 (트리거 A OR B).

A 우선: A·B 동시 만족 시 A 우선 (코드 흐름상 A 먼저 검사).

설계:
  A) 순차 상승   : 강도 80 → 90 → 110 (최고치 기준, 출렁임 허용)
  B) 강매수세 포착 : 다음 3개 동시 만족
       (B1) 강도 ≥100 을 1분 이상 끊김 없이 유지
       (B2) 매도잔량 ≥ 매수잔량 × 1.5 (저항 매물 두꺼움 + 매수세가 잡아먹는 구도)
       (B3) 최근 30초 누적 매수체결대금 ≥ 3억원 (체결강도 헛수 필터)
            "매수체결" = 강도 ≥100 인 체결만 (단순 분류)

매수 후 정책 (strategy_manager):
  A: 손절 -3%(가격), 익절 트레일링, 20MA·시간정리 유지
  B: 손절 시가-1%, 익절 트레일링, 20MA·시간정리 비활성화

trigger 정보 노출:
  get_trigger_info(code) -> {"trigger": "A"|"B"|None, "opening_price": float}

체결강도: FID 228. 시가: parsed_trade 폴백.
"""
import logging
from collections import deque
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ── 트리거 A (순차 상승) ──
LEVELS_A = (80.0, 90.0, 110.0)

# ── 트리거 B (강매수세 포착) ──
B_STRENGTH_THRESHOLD = 100.0
B_HOLD_DURATION = timedelta(minutes=1)            # 1분 유지 (2분→1분)
B_ASKBID_RATIO = 1.5                               # 매도잔량/매수잔량 ≥ 1.5
B_BUY_VALUE_WINDOW = timedelta(seconds=30)         # 30초 슬라이딩 윈도우
B_BUY_VALUE_THRESHOLD = 300_000_000                # 3억원
B_BUY_STRENGTH_MIN = 100.0                         # 매수체결로 카운트할 강도 하한

# ── 공통 ──
MAX_TOTAL_WAIT = timedelta(minutes=7)

# 가격 / 시가 폴백 키
_PRICE_KEYS = ("price", "cur_prc", "current_price", "last_price", "체결가", "10")
_OPEN_KEYS = ("open", "open_price", "opening_price", "시가", "16")
# 호가 잔량 키 폴백 (FID 121=매도총잔량, 125=매수총잔량 기준)
_ASK_QTY_KEYS = ("121", "ask_total_qty", "total_ask_qty", "매도총잔량", "ask_qty")
_BID_QTY_KEYS = ("125", "bid_total_qty", "total_bid_qty", "매수총잔량", "bid_qty")
# 체결수량 키 폴백
_TRADE_QTY_KEYS = ("quantity", "qty", "volume", "체결량", "15")

_raw_trade_keys_logged = False
_raw_orderbook_keys_logged = False


def _extract_field(parsed: dict, keys: tuple) -> float:
    for k in keys:
        v = parsed.get(k)
        if v in (None, ""):
            continue
        try:
            return abs(float(v))
        except (TypeError, ValueError):
            continue
    return 0.0


class Phase3State(Enum):
    WATCHING_120 = "WATCHING"
    WATCHING_150 = "WATCHING"
    WATCHING_180 = "WATCHING"
    HOLD_180     = "WATCHING"
    WATCHING     = "WATCHING"
    READY_TO_BUY = "READY_TO_BUY"
    ABANDONED    = "ABANDONED"


class Phase3Controller:
    def __init__(self, now_func=None):
        self._now = now_func or datetime.now
        self.watched: dict[str, dict] = {}

    def is_watching(self, code: str) -> bool:
        return (code in self.watched
                and self.watched[code]["state"] not in (
                    Phase3State.READY_TO_BUY, Phase3State.ABANDONED))

    def start_watching(self, code: str):
        if code in self.watched:
            return
        self.watched[code] = {
            "start_time":       self._now(),
            "state":            Phase3State.WATCHING,
            "trigger":          None,
            "opening_price":    0.0,
            # 트리거 A
            "stage":            0,
            # 트리거 B
            "hold_start":       None,           # 강도 ≥100 윈도우 시작 시각
            "ask_bid_ratio":    0.0,            # 최신 매도잔량/매수잔량
            "buy_value_window": deque(),        # (시각, 체결대금) 30초 슬라이딩
            "buy_value_sum":    0.0,            # 윈도우 합계 캐시
            # 디버그
            "last_strength":    0.0,
            "max_strength":     0.0,
        }
        logger.info(
            "[%s] 📡 Phase 3 감시 시작 "
            "(A: %s 순차 / B: ≥%.0f 1분유지 + 매도잔량≥매수잔량×%.1f + 30초누적매수≥%.0f억)",
            code,
            "→".join(str(int(x)) for x in LEVELS_A),
            B_STRENGTH_THRESHOLD, B_ASKBID_RATIO, B_BUY_VALUE_THRESHOLD / 1e8,
        )

    def stop_watching(self, code: str):
        self.watched.pop(code, None)

    def get_state(self, code: str) -> Optional[Phase3State]:
        s = self.watched.get(code)
        return s["state"] if s else None

    def get_trigger_info(self, code: str) -> dict:
        s = self.watched.get(code)
        if not s:
            return {"trigger": None, "opening_price": 0.0}
        return {
            "trigger": s.get("trigger"),
            "opening_price": s.get("opening_price", 0.0),
        }

    def _advance_stage(self, st: dict, strength: float) -> bool:
        advanced = False
        while st["stage"] < len(LEVELS_A) and strength >= LEVELS_A[st["stage"]]:
            st["stage"] += 1
            advanced = True
        return advanced

    def _update_buy_value_window(self, st: dict, now: datetime, value: float):
        """30초 슬라이딩 윈도우에 체결대금 추가하고 오래된 거 제거."""
        st["buy_value_window"].append((now, value))
        st["buy_value_sum"] += value
        cutoff = now - B_BUY_VALUE_WINDOW
        while st["buy_value_window"] and st["buy_value_window"][0][0] < cutoff:
            _, old_val = st["buy_value_window"].popleft()
            st["buy_value_sum"] -= old_val
        if st["buy_value_sum"] < 0:
            st["buy_value_sum"] = 0.0  # 부동소수점 누적오차 가드

    def _prune_buy_value_window(self, st: dict, now: datetime):
        """체결 안 들어와도 윈도우 자체는 시간 흐르면 비워야 함."""
        cutoff = now - B_BUY_VALUE_WINDOW
        while st["buy_value_window"] and st["buy_value_window"][0][0] < cutoff:
            _, old_val = st["buy_value_window"].popleft()
            st["buy_value_sum"] -= old_val
        if st["buy_value_sum"] < 0:
            st["buy_value_sum"] = 0.0

    def on_orderbook(self, parsed_orderbook: dict) -> None:
        """0D 호가 콜백. 매도/매수 총잔량 비율만 갱신. 매수 판정은 on_trade에서."""
        global _raw_orderbook_keys_logged

        code = parsed_orderbook.get("stock_code")
        if not code or code not in self.watched:
            return
        st = self.watched[code]
        if st["state"] in (Phase3State.READY_TO_BUY, Phase3State.ABANDONED):
            return

        ask_qty = _extract_field(parsed_orderbook, _ASK_QTY_KEYS)
        bid_qty = _extract_field(parsed_orderbook, _BID_QTY_KEYS)

        if not _raw_orderbook_keys_logged:
            logger.info(
                "[%s] 🔑 호가 raw 키: %s (추출 ask_qty=%.0f, bid_qty=%.0f)",
                code, list(parsed_orderbook.keys()), ask_qty, bid_qty,
            )
            _raw_orderbook_keys_logged = True

        if bid_qty > 0:
            st["ask_bid_ratio"] = ask_qty / bid_qty

    def on_trade(self, parsed_trade: dict) -> Optional[Phase3State]:
        global _raw_trade_keys_logged

        code = parsed_trade.get("stock_code")
        if not code or code not in self.watched:
            return None

        st = self.watched[code]
        if st["state"] in (Phase3State.READY_TO_BUY, Phase3State.ABANDONED):
            return st["state"]

        strength = float(parsed_trade.get("strength") or 0)
        price = _extract_field(parsed_trade, _PRICE_KEYS)
        opening = _extract_field(parsed_trade, _OPEN_KEYS)
        qty = _extract_field(parsed_trade, _TRADE_QTY_KEYS)
        now = self._now()
        st["last_strength"] = strength
        if strength > st["max_strength"]:
            st["max_strength"] = strength
        if opening > 0:
            st["opening_price"] = opening

        if not _raw_trade_keys_logged:
            logger.info(
                "[%s] 🔑 체결 raw 키: %s "
                "(추출 price=%.0f, opening=%.0f, qty=%.0f, strength=%.1f)",
                code, list(parsed_trade.keys()), price, opening, qty, strength,
            )
            _raw_trade_keys_logged = True

        # ── 매수체결 누적 (B3) ──
        # 강도 ≥100 인 체결만 매수체결로 분류해서 누적
        if strength >= B_BUY_STRENGTH_MIN and price > 0 and qty > 0:
            self._update_buy_value_window(st, now, price * qty)
        else:
            self._prune_buy_value_window(st, now)

        # ── 트리거 A: 순차 상승 (우선) ──
        if self._advance_stage(st, strength):
            done = st["stage"]
            if done < len(LEVELS_A):
                logger.info(
                    "[%s] Phase 3-A: %.0f 통과 (%d/%d단계, str=%.1f)",
                    code, LEVELS_A[done - 1], done, len(LEVELS_A), strength,
                )

        if st["stage"] >= len(LEVELS_A):
            st["state"] = Phase3State.READY_TO_BUY
            st["trigger"] = "A"
            logger.info(
                "[%s] Phase 3-A 완료 → READY_TO_BUY (80→90→110, max=%.1f)",
                code, st["max_strength"],
            )
            return st["state"]

        # ── 트리거 B: 3개 조건 동시 만족 ──
        # B1: 강도 ≥100 1분 유지
        if strength < B_STRENGTH_THRESHOLD:
            if st["hold_start"] is not None:
                logger.debug(
                    "[%s] Phase 3-B 강도 윈도우 리셋(%.1f < %.0f)",
                    code, strength, B_STRENGTH_THRESHOLD,
                )
            st["hold_start"] = None
        else:
            if st["hold_start"] is None:
                st["hold_start"] = now
                logger.info(
                    "[%s] Phase 3-B 강도 윈도우 시작 (str=%.1f)",
                    code, strength,
                )
            else:
                # B1 충족 여부
                b1_ok = (now - st["hold_start"]) >= B_HOLD_DURATION
                if b1_ok:
                    # B2: 호가 잔량 비율
                    ratio = st.get("ask_bid_ratio", 0.0)
                    b2_ok = ratio >= B_ASKBID_RATIO

                    # B3: 30초 누적 매수체결
                    b3_ok = st["buy_value_sum"] >= B_BUY_VALUE_THRESHOLD

                    if b2_ok and b3_ok:
                        st["state"] = Phase3State.READY_TO_BUY
                        st["trigger"] = "B"
                        logger.info(
                            "[%s] Phase 3-B 완료 → READY_TO_BUY "
                            "(강도≥%.0f %ds유지 / 매도:매수=%.2f / "
                            "30초누적매수=%.1f억, 시가=%.0f)",
                            code, B_STRENGTH_THRESHOLD,
                            int(B_HOLD_DURATION.total_seconds()),
                            ratio, st["buy_value_sum"] / 1e8,
                            st["opening_price"],
                        )
                        return st["state"]
                    else:
                        # 1분 유지는 됐는데 다른 조건이 안 됨 → 디버그용 1회 로그
                        # (잦으면 noisy하니 debug 레벨)
                        logger.debug(
                            "[%s] Phase 3-B 대기중 (B1=OK, B2 ratio=%.2f/%.2f, "
                            "B3 누적=%.1f억/%.0f억)",
                            code, ratio, B_ASKBID_RATIO,
                            st["buy_value_sum"] / 1e8,
                            B_BUY_VALUE_THRESHOLD / 1e8,
                        )

        # ── 공통: 7분 초과 폐기 ──
        if now - st["start_time"] > MAX_TOTAL_WAIT:
            st["state"] = Phase3State.ABANDONED
            b1_str = (st["hold_start"].strftime("%H:%M:%S")
                      if st["hold_start"] else "None")
            logger.info(
                "[%s] Phase 3 폐기: 7분 미충족 "
                "(A=%d/%d, B1_start=%s, ratio=%.2f, 30초누적=%.1f억, max_str=%.1f)",
                code, st["stage"], len(LEVELS_A), b1_str,
                st.get("ask_bid_ratio", 0.0),
                st["buy_value_sum"] / 1e8,
                st["max_strength"],
            )

        return st["state"]

    def tick(self) -> list[str]:
        now = self._now()
        for code, st in list(self.watched.items()):
            if st["state"] != Phase3State.WATCHING:
                continue
            # 체결 안 들어와도 윈도우는 시간으로 비워줘야 함
            self._prune_buy_value_window(st, now)
            if now - st["start_time"] > MAX_TOTAL_WAIT:
                st["state"] = Phase3State.ABANDONED
                logger.info(
                    "[%s] Phase 3 폐기(tick): 7분 미충족 (A=%d/%d, max=%.1f)",
                    code, st["stage"], len(LEVELS_A), st["max_strength"],
                )
        return []