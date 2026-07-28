import time
from core.order_manager import OrderManager
from utils.logger import logger

# 전략 매핑 (동일)
CONDITION_STRATEGIES = {
    "주도주상위": {"sizing": "MAX_VOLUME", "exit_strategy": "TRAILING_EXIT"},
    "체결강도100": {"sizing": "REGULAR_VOLUME", "exit_strategy": "REGULAR"},
    "돌파자동매매용": {"sizing": "MAX_VOLUME", "exit_strategy": "TRAILING_EXIT"},
    "관심종목감시": {"sizing": "REGULAR_VOLUME", "exit_strategy": "REGULAR"},
    "DEFAULT": {"sizing": "REGULAR_VOLUME", "exit_strategy": "REGULAR"},
}


class ConditionManager:
    def __init__(self, order_mgr: OrderManager):
        self.order_mgr = order_mgr
        self.condition_map: dict[str, str] = {}  # {종목코드: 조건식명}

    def update_snapshot(self, seq: str, condition_name: str, code_list: list):
        """kiwoom_ws.py에서 스냅샷 정보를 받아 매핑 업데이트"""
        for code in code_list:
            self.condition_map[code] = condition_name
        logger.info(f"💾 [매핑] {condition_name}({len(code_list)}개) 스냅샷 매핑 완료")

    async def handle_signal(self, stock_code: str, signal_type: str, raw: dict):
        stock_code = self._normalize_code(stock_code)
        if not stock_code:
            return

        if signal_type == "D":
            return

        # 1. 매핑된 조건식 이름 찾기
        condition_name = self.condition_map.get(stock_code, "DEFAULT")

        # 2. 전략 매핑
        strategy = CONDITION_STRATEGIES.get(
            condition_name, CONDITION_STRATEGIES["DEFAULT"]
        )

        # 3. 로그 출력 (seq=?가 사라져야 합니다)
        logger.info(
            f"📈 [편입] {stock_code} ({condition_name}) → 전략: {strategy['sizing']}"
        )

        try:
            self.order_mgr.try_buy(
                stock_code,
                sizing=strategy["sizing"],
                exit_strategy=strategy["exit_strategy"],
            )
        except Exception as e:
            logger.error(f"매수 처리 중 예외: {e}")

    @staticmethod
    def _normalize_code(code: str) -> str:
        code = code.replace("A", "").strip()
        return code if len(code) == 6 else ""
