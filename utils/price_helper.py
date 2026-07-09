"""
한국 주식 호가단위(틱) 계산 헬퍼
"""


def get_tick_size(price: int) -> int:
    """가격대별 호가단위 반환"""
    if price < 2_000:
        return 1
    elif price < 5_000:
        return 5
    elif price < 20_000:
        return 10
    elif price < 50_000:
        return 50
    elif price < 200_000:
        return 100
    elif price < 500_000:
        return 500
    else:
        return 1_000


def round_to_tick(price: int) -> int:
    """주어진 가격을 호가단위에 맞게 반올림"""
    if price <= 0:
        return 0
    tick = get_tick_size(price)
    return (price // tick) * tick


def add_ticks(price: int, n: int) -> int:
    """현재가에서 n틱 위/아래 가격 계산
    n>0: 위(매수 호가), n<0: 아래(매도 호가)
    """
    if price <= 0:
        return 0
    result = price
    for _ in range(abs(n)):
        tick = get_tick_size(result)
        result = result + tick if n > 0 else result - tick
    return result