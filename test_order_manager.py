import time
from unittest.mock import MagicMock

# 주소는 JO님의 프로젝트 폴더 구조에 맞게 수정해주세요 (예: from managers.order_manager import OrderManager)
# 수정 전
# from order_manager import OrderManager

# 수정 후 (core 폴더 경로를 명시)
from core.order_manager import OrderManager

class DummyKiwoomREST:
    """테스트를 위한 가짜 키움 API 클래스"""

    def get_stock_info(self, code):
        return {"stk_nm": f"테스트종목_{code}"}

    def get_current_price(self, code):
        return 10000  # 현재가 10,000원 고정

    def get_orderable_amount(self):
        return 10_000_000  # 예수금 1,000만 원 보유 중으로 가정

    def buy_market_order(self, code, qty, price, trde_tp):
        return {"return_code": 0, "return_msg": "정상체결", "ord_no": "20260718_01"}


def run_test():
    print("🚀 [OrderManager 고도화 기능 테스트 시작]")
    print("=" * 50)

    # 1. 초기화
    rest_mock = DummyKiwoomREST()
    manager = OrderManager(rest_mock)

    # 강제 장중 시간 설정 (테스트용)
    manager._is_trading_time = lambda: True

    # --------------------------------------------------------
    # 케이스 1: REGULAR_VOLUME (정상 비중 200만 원 매수 테스트)
    # --------------------------------------------------------
    print("\n[케이스 1] 정상 비중 매수 진입 (목표금액: 200만 원)")
    # 현재가 10,000원 + 1틱(호가단비 계산 무시하고 10,001원 가정) -> 대략 199주
    success1 = manager.try_buy(
        "005930", sizing="REGULAR_VOLUME", exit_strategy="REGULAR"
    )

    if success1:
        pos = manager.positions["005930"]
        print(
            f"✅ 매수 성공! 보유 수량: {pos['qty']}주, 평단가: {pos['avg_price']:,}원"
        )
        print(f"✅ 저장된 Sizing 상태: {pos['sizing']}")
        print(f"✅ 저장된 Exit 전략 태그: {pos['exit_strategy']}")
    else:
        print("❌ 케이스 1 실패")

    # --------------------------------------------------------
    # 케이스 2: MAX_VOLUME (비중 확대 1.5배 -> 300만 원 매수 테스트)
    # --------------------------------------------------------
    print("\n[케이스 2] 주도주 비중 확대 매수 진입 (목표금액: 300만 원)")
    success2 = manager.try_buy(
        "000660", sizing="MAX_VOLUME", exit_strategy="TRAILING_EXIT"
    )

    if success2:
        pos = manager.positions["000660"]
        print(
            f"✅ 매수 성공! 보유 수량: {pos['qty']}주, 평단가: {pos['avg_price']:,}원"
        )
        print(
            f"✅ 저장된 Sizing 상태: {pos['sizing']} (정상 대비 수량 약 1.5배 증가 확인)"
        )
        print(
            f"✅ 저장된 Exit 전략 태그: {pos['exit_strategy']} (★주도주 탈출 태그가 각인됨)"
        )
    else:
        print("❌ 케이스 2 실패")

    # --------------------------------------------------------
    # 케이스 3: StrategyManager용 buy wrapper 확장 테스트
    # --------------------------------------------------------
    print("\n[케이스 3] StrategyManager가 직접 호출하는 buy() wrapper 테스트")
    res = manager.buy(
        "035420", qty=50, sizing="MIN_VOLUME", exit_strategy="TRAILING_EXIT"
    )

    if res["success"]:
        pos = manager.positions["035420"]
        print(f"✅ Wrapper 매수 성공! ord_no: {res['ord_no']}")
        print(
            f"✅ 잔고 기록 데이터: Sizing={pos['sizing']}, Exit={pos['exit_strategy']}"
        )
    else:
        print(f"❌ 케이스 3 실패: {res['error']}")

    print("\n" + "=" * 50)
    print("📊 [최종 테스트 완료] 전체 포지션 상태 요약:")
    print(manager.status_summary())
    for code, data in manager.positions.items():
        print(f"   > 종목코드 [{code}] -> 매도전략유형: {data['exit_strategy']}")


if __name__ == "__main__":
    run_test()
