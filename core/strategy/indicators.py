"""
기술적 지표 계산 모듈
─────────────────────────────────────
이동평균선(MA), MA 터치 판정 등
본인 전략의 핵심 지표 계산 함수들.
"""


def calc_ma(candles: list[dict], period: int) -> float:
    """
    이동평균선 (Simple Moving Average) 계산.
    
    Args:
        candles: 분봉 리스트 (시간 역순, [0]이 최신).
                 각 항목은 {'close': int, ...} 형태여야 함.
        period: 기간 (예: 5, 20, 60, 120).
    
    Returns:
        직전 period개 봉의 종가 평균.
        봉이 부족하면 0.
    
    Examples:
        >>> candles = [{'close': 100}, {'close': 102}, {'close': 98},
        ...            {'close': 105}, {'close': 103}]
        >>> calc_ma(candles, 5)
        101.6
    """
    if len(candles) < period:
        return 0.0
    closes = [c["close"] for c in candles[:period]]
    return sum(closes) / period


def is_ma_touch(price: int, ma: float, tolerance_pct: float = 0.3) -> bool:
    """
    현재가가 이동평균선에 '터치'했는지 판정.
    
    터치 정의: 현재가가 MA의 ±tolerance_pct% 이내.
    
    Args:
        price: 현재가.
        ma: 이동평균값.
        tolerance_pct: 허용 범위 (%). 0.3이면 ±0.3%.
    
    Returns:
        True면 터치.
    
    Examples:
        >>> is_ma_touch(100, 100.0, 0.3)   # 정확히 터치
        True
        >>> is_ma_touch(100, 100.2, 0.3)   # +0.2% 차이 → 터치
        True
        >>> is_ma_touch(100, 100.5, 0.3)   # +0.5% 차이 → 미터치
        False
    """
    if ma == 0:
        return False
    diff_pct = abs(price - ma) / ma * 100
    return diff_pct <= tolerance_pct


def is_volume_breakout(
    candles: list[dict],
    multiplier: float = 1.5,
    avg_window: int = 5,
) -> bool:
    """
    최근 봉의 거래량이 직전 평균의 multiplier배 이상인지 판정.
    
    Args:
        candles: 분봉 리스트 (시간 역순, [0]이 최신).
        multiplier: 배수 (1.5이면 평균의 1.5배 이상).
        avg_window: 평균 계산할 봉 개수 (5이면 [1:6] 사용).
    
    Returns:
        True면 거래량 폭증.
    
    Examples:
        >>> candles = [{'volume': 1000}, {'volume': 500}, {'volume': 600},
        ...            {'volume': 550}, {'volume': 450}, {'volume': 500}]
        >>> is_volume_breakout(candles, 1.5)   # 1000 vs 평균 520
        True
    """
    if len(candles) < avg_window + 1:
        return False
    
    latest_vol = candles[0]["volume"]
    prev_volumes = [c["volume"] for c in candles[1:1 + avg_window]]
    
    if not prev_volumes:
        return False
    
    avg_vol = sum(prev_volumes) / len(prev_volumes)
    if avg_vol == 0:
        return False
    
    return latest_vol >= avg_vol * multiplier


def get_volume_ratio(
    candles: list[dict],
    avg_window: int = 5,
) -> float:
    """
    최근 봉 거래량 / 직전 평균 거래량 비율.
    
    is_volume_breakout()의 진단/로그용 버전.
    
    Returns:
        비율 (예: 2.5 = 평균의 2.5배). 데이터 부족하면 0.
    """
    if len(candles) < avg_window + 1:
        return 0.0
    
    latest_vol = candles[0]["volume"]
    prev_volumes = [c["volume"] for c in candles[1:1 + avg_window]]
    
    if not prev_volumes:
        return 0.0
    
    avg_vol = sum(prev_volumes) / len(prev_volumes)
    if avg_vol == 0:
        return 0.0
    
    return latest_vol / avg_vol


def is_bullish_candle(candle: dict) -> bool:
    """
    양봉(상승 봉) 여부.
    
    Args:
        candle: 봉 dict, {'open': int, 'close': int, ...}.
    
    Returns:
        종가 > 시가면 True.
    """
    return candle["close"] > candle["open"]


def is_bearish_candle(candle: dict) -> bool:
    """음봉 여부 (참고용)"""
    return candle["close"] < candle["open"]


def get_today_first_candle(candles: list[dict], target_date: str) -> dict | None:
    """
    당일 첫 1분봉 (=시초가 봉) 찾기.
    
    Args:
        candles: 분봉 리스트 (시간 역순).
        target_date: YYYYMMDD.
    
    Returns:
        오늘 09:01봉 dict (또는 가장 이른 오늘 봉).
        없으면 None.
    """
    today_candles = [c for c in candles if c["time_str"].startswith(target_date)]
    if not today_candles:
        return None
    
    # 시간 역순이니 마지막이 가장 이른 봉
    return today_candles[-1]


def calc_rise_pct_from_open(current: int, today_open: int) -> float:
    """
    시초가 대비 상승률 (%).
    
    Args:
        current: 현재가.
        today_open: 오늘 시초가.
    
    Returns:
        상승률 (예: 5.3 = +5.3%). today_open=0이면 0.0.
    """
    if today_open == 0:
        return 0.0
    return (current - today_open) / today_open * 100

def calc_obv(candles: list[dict]) -> list[float]:
    """
    OBV(On-Balance Volume, 누적거래량) 계산.
    종가가 전봉보다 오르면 +거래량, 내리면 -거래량, 같으면 변화 없음으로 누적.

    Args:
        candles: 분봉 리스트 (시간 역순, [0]이 최신). 반드시 당일 데이터만
                 넣을 것 — 전일 데이터가 섞이면 하루 경계에서 누적 기준선이
                 왜곡됨(VWAP과 동일한 주의사항, core/strategy_manager.py의
                 _apply_vwap_filter 참고).

    Returns:
        OBV 누적값 리스트, 과거->최신 순(= candles와 반대 방향, [-1]이 최신).
        candles가 2개 미만이면 빈 리스트.

    Examples:
        >>> candles = [{'close': 103, 'volume': 50}, {'close': 100, 'volume': 40},
        ...            {'close': 98, 'volume': 30}]
        >>> calc_obv(candles)
        [0.0, -30.0, 10.0]
    """
    if len(candles) < 2:
        return []
    chron = list(reversed(candles))  # 과거 -> 최신 순으로 뒤집음
    obv = [0.0]
    for i in range(1, len(chron)):
        prev_close = chron[i - 1]["close"]
        cur = chron[i]
        vol = float(cur.get("volume", 0) or 0)
        if cur["close"] > prev_close:
            obv.append(obv[-1] + vol)
        elif cur["close"] < prev_close:
            obv.append(obv[-1] - vol)
        else:
            obv.append(obv[-1])
    return obv


def obv_momentum(candles: list[dict], lookback: int = 5) -> float:
    """
    반등이 거래량(매집)에 뒷받침되는지 OBV로 확인하는 연속값 버전 —
    OBV 변화량을 그 구간 총거래량으로 정규화한 모멘텀(-1.0~1.0).
    +1에 가까울수록 그 구간 내내 상승 쪽으로 거래량이 뒷받침됨(강한 매집),
    -1에 가까울수록 하락 쪽으로 뒷받침됨(매도 우세). 0 이하면 방향성 없음/매집 안 됨.

    Args:
        candles: 분봉 리스트 (시간 역순, [0]이 최신, 당일 데이터만 — calc_obv 참고).
        lookback: 몇 봉 구간의 변화를 볼지.

    Returns:
        데이터가 부족하면 0.0(무점수 — 계산 불가를 유리하지도 불리하지도 않게 처리).

    Examples:
        >>> candles = [{'close': 105, 'volume': 100}, {'close': 103, 'volume': 100},
        ...            {'close': 101, 'volume': 100}, {'close': 100, 'volume': 100},
        ...            {'close': 99, 'volume': 100}, {'close': 98, 'volume': 100}]
        >>> obv_momentum(candles, lookback=5)
        1.0
    """
    obv = calc_obv(candles)
    if len(obv) <= lookback:
        return 0.0
    window = candles[:lookback]  # 최신 lookback개 (시간 역순 그대로)
    total_vol = sum(float(c.get("volume", 0) or 0) for c in window)
    if total_vol <= 0:
        return 0.0
    diff = obv[-1] - obv[-1 - lookback]
    return max(-1.0, min(1.0, diff / total_vol))


def is_volume_increasing_streak(candles: list[dict], streak: int = 3) -> bool:
    """
    거래량 증가 지속 판정: 최근 `streak`개 봉의 거래량이 과거->최신 방향으로
    연속 증가했는지 확인.

    Args:
        candles: 분봉 리스트 (시간 역순, [0]이 최신).
        streak: 연속 증가를 확인할 구간 길이 (3이면 candles[0]>candles[1]>candles[2]>candles[3]).

    Returns:
        True면 거래량이 streak개 봉 동안 지속 증가.

    Examples:
        >>> candles = [{'volume': 400}, {'volume': 300}, {'volume': 200}, {'volume': 100}]
        >>> is_volume_increasing_streak(candles, 3)
        True
    """
    if not candles or len(candles) < streak + 1:
        return False
    recent = [float(c.get("volume", 0) or 0) for c in candles[: streak + 1]]
    for i in range(streak):
        if recent[i] <= recent[i + 1]:
            return False
    return True
