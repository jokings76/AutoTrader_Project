"""
분봉 조회 메서드 테스트.

실행: python test_minute_candles.py
"""
from api.auth import get_access_token
from api.kiwoom_rest import KiwoomREST
from config import settings


def main():
    # 토큰 발급
    token = get_access_token()
    if not token:
        print("❌ 토큰 발급 실패")
        return
    print(f"✅ 토큰 발급 완료: {token[:20]}...")

    rest = KiwoomREST(token, is_mock=settings.IS_MOCK)

    # ───── 테스트 1: 1분봉 5개 ─────
    print("\n" + "=" * 60)
    print("테스트 1: 삼성전자(005930) 1분봉 5개 (최신순)")
    print("=" * 60)

    candles = rest.get_minute_candles("005930", interval=1, count=5)
    print(f"받은 봉 개수: {len(candles)}")

    for i, c in enumerate(candles):
        diff = c["close"] - c["open"]
        if diff > 0:
            sign = f"양봉 +{diff:,}원"
        elif diff < 0:
            sign = f"음봉 {diff:,}원"
        else:
            sign = "보합"
        print(f"\n[{i}] {c['time_str']}")
        print(f"  시:{c['open']:>9,}  고:{c['high']:>9,}  "
              f"저:{c['low']:>9,}  종:{c['close']:>9,}")
        print(f"  거래량: {c['volume']:>10,}  ({sign})")

    # ───── 테스트 2: 60분봉 5개 ─────
    print("\n" + "=" * 60)
    print("테스트 2: 삼성전자(005930) 60분봉 5개")
    print("=" * 60)

    candles_60 = rest.get_minute_candles("005930", interval=60, count=5)
    print(f"받은 봉 개수: {len(candles_60)}")

    for i, c in enumerate(candles_60):
        print(f"  [{i}] {c['time_str']}  "
              f"시:{c['open']:,}  고:{c['high']:,}  "
              f"저:{c['low']:,}  종:{c['close']:,}")

    # ───── 테스트 3: 09:00 60분봉 시가 (참고용) ─────
    print("\n" + "=" * 60)
    print("테스트 3: 09:00 60분봉 시가 조회")
    print("=" * 60)

    targets = [
        ("005930", "삼성전자"),
        ("000660", "SK하이닉스"),
        ("042700", "한미반도체"),
    ]
    for code, name in targets:
        op = rest.get_today_60min_open(code)
        print(f"  {name:8s} ({code}): {op:>10,}원")

    # ───── 테스트 4: 이동평균선 계산 ─────
    print("\n" + "=" * 60)
    print("테스트 4: 5MA / 60MA 계산 (삼성전자)")
    print("=" * 60)

    candles_70 = rest.get_minute_candles("005930", interval=1, count=70)
    print(f"받은 1분봉 개수: {len(candles_70)}")

    if len(candles_70) >= 5:
        ma5_closes = [c["close"] for c in candles_70[:5]]
        ma5 = sum(ma5_closes) / 5
        print(f"  5MA  = {ma5:>10,.0f}원  (최근 5봉 평균)")

    if len(candles_70) >= 60:
        ma60_closes = [c["close"] for c in candles_70[:60]]
        ma60 = sum(ma60_closes) / 60
        print(f"  60MA = {ma60:>10,.0f}원  (최근 60봉 평균)")

    if len(candles_70) > 0:
        current = candles_70[0]["close"]
        print(f"  현재가  = {current:>10,}원")
        if len(candles_70) >= 5:
            diff5 = (current - ma5) / ma5 * 100
            print(f"  5MA  대비: {diff5:+.2f}%")
        if len(candles_70) >= 60:
            diff60 = (current - ma60) / ma60 * 100
            print(f"  60MA 대비: {diff60:+.2f}%")

    print("\n✅ 모든 테스트 완료")


if __name__ == "__main__":
    main()