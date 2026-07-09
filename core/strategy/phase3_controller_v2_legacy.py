"""
Phase 3 컨트롤러 — 체결강도 기반 진입 (트리거 A OR B).

시간/슬롯/라우팅: StrategyManager 관리, 인터페이스 동일.

설계 (둘 중 하나 만족 시 READY_TO_BUY → 점수 게이트):
  A) 순차 상승   : 강도 80 → 90 → 110 (최고치 기준, 출렁임 허용)
  B) 유지+비하락 : 강도 ≥100 을 2분 이상 끊김 없이 유지
                  + 시작가 대비 -1% 초과 하락 없음 (잔파동 허용)

  공통: 7분 내 둘 다 미달 → ABANDONED.

상태 머신:
  WATCHING → (A 만족 OR B 만족) → READY_TO_BUY
           → (7분 경과)           → ABANDONED

체결강도: 키움 FID 228. 가격: parsed_trade 폴백 (첫 trade에서 키 로그).
"""
import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ── 트리거 A (순차 상승) ──
LEVELS_A = (80.0, 90.0, 110.0)

# ── 트리거 B (유지 + 비하락) ──
HOLD_THRESHOLD = 100.0                  # 강도 기준
HOLD_DURATION = timedelta(minutes=2)    # 유지 시간
HOLD_MAX_DROP = 0.01                    # 시작가 대비 -1% 초과 하락 시 리셋

# ── 공통 ──
MAX_TOTAL_WAIT = timedelta(minutes=7)

# 가격 키 폴백 (첫 trade 로그 본 뒤 정확한 키 한 개로 줄여도 됨)
_PRICE_KEYS = ("price", "cur_prc", "current_price", "last_price", "체결가", "10")
_raw_keys_logged = False  # 모듈 1회 로깅


def _extract_price(parsed: dict) -> float:
    for k in _PRICE_KEYS:
        v = parsed.get(k)
        if v in (None, ""):
            continue
        try:
            return abs(float(v))  # FID 10은 부호 붙어올 수 있음
        except (TypeError, ValueError):
            continue
    return 0.0


class Phase3State(Enum):
    # 구버전 호환 별칭 유지
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
            # 트리거 A
            "stage":            0,
            # 트리거 B
            "hold_start":       None,   # None=미시작
            "hold_start_price": 0.0,
            # 디버그
            "last_strength":    0.0,
            "max_strength":     0.0,
        }
        a = "→".join(str(int(x)) for x in LEVELS_A)
        logger.info(
            "[%s] 📡 Phase 3 감시 시작 (A: %s 순차 / B: ≥%.0f 2분유지+-1%%이내)",
            code, a, HOLD_THRESHOLD,
        )

    def stop_watching(self, code: str):
        self.watched.pop(code, None)

    def get_state(self, code: str) -> Optional[Phase3State]:
        s = self.watched.get(code)
        return s["state"] if s else None

    def _advance_stage(self, st: dict, strength: float) -> bool:
        advanced = False
        while st["stage"] < len(LEVELS_A) and strength >= LEVELS_A[st["stage"]]:
            st["stage"] += 1
            advanced = True
        return advanced

    def on_trade(self, parsed_trade: dict) -> Optional[Phase3State]:
        global _raw_keys_logged

        code = parsed_trade.get("stock_code")
        if not code or code not in self.watched:
            return None

        st = self.watched[code]
        if st["state"] in (Phase3State.READY_TO_BUY, Phase3State.ABANDONED):
            return st["state"]

        strength = float(parsed_trade.get("strength") or 0)
        price = _extract_price(parsed_trade)
        now = self._now()
        st["last_strength"] = strength
        if strength > st["max_strength"]:
            st["max_strength"] = strength

        # 첫 trade에서 키 1회 로깅 (가격 키 확인용)
        if not _raw_keys_logged:
            logger.info(
                "[%s] 🔑 체결 raw 키: %s (추출 price=%.0f, strength=%.1f)",
                code, list(parsed_trade.keys()), price, strength,
            )
            _raw_keys_logged = True

        # ── 트리거 A: 순차 상승 ──
        if self._advance_stage(st, strength):
            done = st["stage"]
            if done < len(LEVELS_A):
                logger.info(
                    "[%s] Phase 3-A: %.0f 통과 (%d/%d단계, str=%.1f)",
                    code, LEVELS_A[done - 1], done, len(LEVELS_A), strength,
                )

        if st["stage"] >= len(LEVELS_A):
            st["state"] = Phase3State.READY_TO_BUY
            logger.info(
                "[%s] Phase 3-A 완료 → READY_TO_BUY (80→90→110, max=%.1f)",
                code, st["max_strength"],
            )
            return st["state"]

        # ── 트리거 B: 강도 ≥100 + 시작가 -1% 이내 + 2분 유지 ──
        if strength < HOLD_THRESHOLD:
            # 강도 끊김 → 윈도우 리셋
            if st["hold_start"] is not None:
                logger.debug(
                    "[%s] Phase 3-B 윈도우 리셋(강도 %.1f < %.0f)",
                    code, strength, HOLD_THRESHOLD,
                )
            st["hold_start"] = None
            st["hold_start_price"] = 0.0
        else:
            if st["hold_start"] is None:
                # 윈도우 시작
                st["hold_start"] = now
                st["hold_start_price"] = price
                logger.info(
                    "[%s] Phase 3-B 윈도우 시작 (str=%.1f, price=%.0f)",
                    code, strength, price,
                )
            else:
                # 진행 중 → 시작가 대비 -1% 초과 하락 시 리셋 (잔파동 허용)
                drop_pct = 0.0
                if price > 0 and st["hold_start_price"] > 0:
                    drop_pct = (st["hold_start_price"] - price) / st["hold_start_price"]
                if drop_pct > HOLD_MAX_DROP:
                    logger.debug(
                        "[%s] Phase 3-B 윈도우 리셋(가격 -%.2f%% 하락: %.0f→%.0f)",
                        code, drop_pct * 100, st["hold_start_price"], price,
                    )
                    st["hold_start"] = None
                    st["hold_start_price"] = 0.0
                elif now - st["hold_start"] >= HOLD_DURATION:
                    chg_pct = ((price - st["hold_start_price"])
                               / st["hold_start_price"] * 100
                               if st["hold_start_price"] > 0 else 0.0)
                    st["state"] = Phase3State.READY_TO_BUY
                    logger.info(
                        "[%s] Phase 3-B 완료 → READY_TO_BUY "
                        "(2분 ≥%.0f 유지, %.0f→%.0f, %+.2f%%)",
                        code, HOLD_THRESHOLD,
                        st["hold_start_price"], price, chg_pct,
                    )
                    return st["state"]

        # ── 공통: 7분 초과 폐기 ──
        if now - st["start_time"] > MAX_TOTAL_WAIT:
            st["state"] = Phase3State.ABANDONED
            b_str = (st["hold_start"].strftime("%H:%M:%S")
                     if st["hold_start"] else "None")
            logger.info(
                "[%s] Phase 3 폐기: 7분 미충족 "
                "(A=%d/%d, B_start=%s, max_str=%.1f)",
                code, st["stage"], len(LEVELS_A), b_str, st["max_strength"],
            )

        return st["state"]

    def tick(self) -> list[str]:
        """체결 안 와도 7분 경과 시 폐기. READY 승격은 on_trade에서만."""
        now = self._now()
        for code, st in list(self.watched.items()):
            if st["state"] == Phase3State.WATCHING:
                if now - st["start_time"] > MAX_TOTAL_WAIT:
                    st["state"] = Phase3State.ABANDONED
                    logger.info(
                        "[%s] Phase 3 폐기(tick): 7분 미충족 "
                        "(A=%d/%d, max=%.1f)",
                        code, st["stage"], len(LEVELS_A), st["max_strength"],
                    )
        return []