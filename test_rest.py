"""
KiwoomREST 단독 테스트
"""
from api.auth import get_access_token
from api.kiwoom_rest import KiwoomREST


def main():
    print("=" * 50)
    print("🧪 KiwoomREST 단독 테스트")
    print("=" * 50)

    # 1) 토큰
    token = get_access_token()
    if not token:
        print("❌ 토큰 발급 실패")
        return

    rest = KiwoomREST(token, is_mock=True)

    # 2) 예수금 조회
    print("\n[1] 예수금 조회")
    deposit = rest.get_deposit()
    print(f"  return_code: {deposit.get('return_code')}")
    print(f"  예수금:        {rest._safe_int(deposit.get('entr')):>12,} 원")
    print(f"  주문가능금액:  {rest._safe_int(deposit.get('ord_alow_amt')):>12,} 원")

    # 3) 주문가능금액만 깔끔하게
    print(f"\n[2] 주문가능금액 (헬퍼): {rest.get_orderable_amount():,} 원")

    # 4) 보유종목
    print("\n[3] 보유종목 조회")
    holdings = rest.get_holdings()
    if holdings:
        for code, info in holdings.items():
            print(f"  {info['name']} ({code}): "
                  f"{info['qty']}주 @ {info['avg_price']:,}원 "
                  f"(평가손익 {info['pnl']:+,}원, {info['pnl_rate']:+.2f}%)")
    else:
        print("  보유종목 없음")

    # 5) 현재가 조회 (삼성전자)
    print("\n[4] 삼성전자(005930) 현재가")
    price = rest.get_current_price("005930")
    print(f"  현재가: {price:,} 원")

    # 6) ⚠️ 매수 주문 테스트 (모의투자에서만!)
    #    실행하고 싶으면 아래 주석 해제
    # print("\n[5] 삼성전자 1주 시장가 매수 (모의투자)")
    # result = rest.buy_market_order("005930", qty=1, trde_tp="3")
    # print(f"  결과: {result}")

    print("\n" + "=" * 50)
    print("테스트 종료")
    print("=" * 50)


if __name__ == "__main__":
    main()