"""
조건검색 신호 매니저
─────────────────────────────────────
역할:
  - WebSocket에서 받은 편입/이탈 신호를 받아 OrderManager로 전달
  - 신호 필터링 (중복/시간/블랙리스트)
  - 신호 통계 기록 (편입/이탈 횟수)
"""
import time
from datetime import datetime
from typing import Optional

from core.order_manager import OrderManager
from utils.logger import logger


# ─────────────────────────────────────
# 신호 처리 파라미터
# ─────────────────────────────────────
DEDUP_WINDOW_SEC = 1.0           # 같은 종목 신호 중복 무시 (1초 이내)
SIGNAL_LOG_INTERVAL_SEC = 60     # 신호 통계 출력 주기

# 종목 블랙리스트 (관리종목, 거래정지, 본인이 싫은 종목)
# 6자리 종목코드 (예: '005930')
BLACKLIST: set[str] = {
    # "123456",  # 예시 - 직접 추가
}


class ConditionManager:
    """편입/이탈 신호 처리"""

    def __init__(self, order_mgr: OrderManager):
        self.order_mgr = order_mgr

        # 중복 신호 필터: {종목코드: 마지막 신호 시각}
        self._last_signal_ts: dict[str, float] = {}

        # 신호 통계
        self._stats = {
            "insert": 0,        # 편입 신호 총 건수
            "delete": 0,        # 이탈 신호 총 건수
            "filtered": 0,      # 필터링된 신호 (중복/블랙리스트 등)
            "buy_attempted": 0, # OrderManager로 매수 시도 횟수
        }
        self._stats_last_print = time.time()

    # ─────────────────────────────────────
    # WebSocket 콜백 진입점
    # ─────────────────────────────────────
    async def handle_signal(self, stock_code: str, signal_type: str, raw: dict):
        """
        KiwoomWS의 on_signal 콜백으로 등록되는 함수.
        signal_type: 'I'(편입) | 'D'(이탈)
        """
        # 1. 종목코드 정규화 ('A005930' → '005930')
        stock_code = self._normalize_code(stock_code)
        if not stock_code:
            return

        # 2. 이탈 신호: 로그만 남기고 종료
        if signal_type == "D":
            self._stats["delete"] += 1
            held = stock_code in self.order_mgr.positions
            held_mark = " (보유중)" if held else ""
            logger.info(f"📉 [이탈] {stock_code}{held_mark}")
            self._maybe_print_stats()
            return

        # 3. 편입 신호 처리
        self._stats["insert"] += 1

        # 3-1. 중복 신호 필터
        if self._is_duplicate(stock_code):
            self._stats["filtered"] += 1
            logger.debug(f"[필터] {stock_code} 중복 신호 무시")
            return

        # 3-2. 블랙리스트 체크
        if stock_code in BLACKLIST:
            self._stats["filtered"] += 1
            logger.info(f"[필터] {stock_code} 블랙리스트")
            return

        # 3-3. 시간대 필터 (장중 아니면 매수 안 보냄, 로그는 남김)
        if not self._is_signal_window():
            self._stats["filtered"] += 1
            logger.info(f"📈 [편입] {stock_code} - 거래시간 외, 매수 보류")
            return

        # ── 모든 필터 통과 ──
        logger.info(f"📈 [편입] {stock_code} → 매수 시도")
        self._stats["buy_attempted"] += 1

        try:
            # OrderManager.try_buy는 동기 함수
            self.order_mgr.try_buy(stock_code)
        except Exception:
            logger.exception(f"매수 처리 중 예외: {stock_code}")

        self._maybe_print_stats()

    # ─────────────────────────────────────
    # 필터 헬퍼
    # ─────────────────────────────────────
    @staticmethod
    def _normalize_code(code: str) -> str:
        """'A005930' → '005930', 공백/이상값 처리"""
        if not code:
            return ""
        code = code.strip()
        if code.startswith("A"):
            code = code[1:]
        # 6자리 숫자가 아니면 무효
        if not (code.isdigit() and len(code) == 6):
            return ""
        return code

    def _is_duplicate(self, stock_code: str) -> bool:
        """같은 종목 신호가 1초 이내 중복으로 들어왔는지"""
        now = time.time()
        last = self._last_signal_ts.get(stock_code, 0)
        if now - last < DEDUP_WINDOW_SEC:
            return True
        self._last_signal_ts[stock_code] = now
        return False

    @staticmethod
    def _is_signal_window() -> bool:
        """
        매수용 신호 처리 가능 시간대.
        OrderManager.TRADING_START~TRADING_END와 별도로 여기서도 한번 거름.
        """
        from core.order_manager import TRADING_START, TRADING_END
        now = datetime.now().strftime("%H:%M")
        return TRADING_START <= now <= TRADING_END

    # ─────────────────────────────────────
    # 통계 출력
    # ─────────────────────────────────────
    def _maybe_print_stats(self):
        """주기적으로 신호 통계 로그"""
        if time.time() - self._stats_last_print < SIGNAL_LOG_INTERVAL_SEC:
            return
        self._stats_last_print = time.time()
        s = self._stats
        logger.info(
            f"📊 [신호통계] 편입={s['insert']} 이탈={s['delete']} "
            f"필터링={s['filtered']} 매수시도={s['buy_attempted']}"
        )

    def get_stats(self) -> dict:
        return dict(self._stats)