"""
OrderManager 단독 테스트 (장 외에서도 흐름 검증 가능)
"""
from api.auth import get_access_token
from api.kiwoom_rest import KiwoomREST
from core.order_manager import OrderManager


def main():
    print("=" * 50)
    print("🧪 OrderManager 단독 테스트")
    print("=" * 50)

    token = get_access_token()
    rest = KiwoomREST(token, is_mock=True)
    om = OrderManager(rest)

    # 1) 잔고 동기화
    print("\n[1] 잔고 동기화")
    om.sync_positions_from_server()
    print(om.status_summary())

    # 2) 주문가능금액
    print(f"\n[2] 주문가능금액: {rest.get_orderable_amount():,}원")

    # 3) 가상 매수 시도 (삼성전자)
    #    장 외 시간엔 실제 주문은 거절되지만, 필터링 로직은 다 작동함
    print("\n[3] 매수 시도 (삼성전자)")
    print("    - 장 외 시간이면 '거래시간 외'로 보류 표시되는 게 정상")
    success = om.try_buy("005930")
    print(f"    결과: {'성공' if success else '보류/실패'}")

    # 4) 매도 모니터링 (현재 보유 중이면 손익 평가)
    print("\n[4] 보유 종목 손익 평가")
    om.check_and_sell_positions()

    print("\n" + "=" * 50)
    print("테스트 종료")
    print("=" * 50)


if __name__ == "__main__":
    main()