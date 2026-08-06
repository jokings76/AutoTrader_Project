"""체결 틱 슬라이딩 윈도우 + 시간가중 체결강도 + 가격 추적.

KiwoomWS의 on_trade 콜백에서 add_tick() 호출.
체결강도 = 가중 매수체결량 / 가중 매도체결량 × 100
가격: get_latest_price / get_price_around / get_price_change_pct
"""
import math
import time
from collections import deque
from typing import Callable, Optional

# 가짜 체결강도 방지 (2026-07-29): 짧은 윈도우(예: 10초) 안에 매도 체결이 우연히
# 0~1건이면 분모가 0에 가까워져 9999나 11896 같은 비현실적인 값이 나옴 —
# 실제로는 "매수세가 강함"이 아니라 "그 순간 매도 체결이 잠깐 안 찍혔다"는
# 우연에 가까움(2026-07-29 실거래에서 entry_strength가 이런 값이었던 종목들이
# 전부 10~13분 안에 강도 0으로 무너지며 손실로 끝난 것으로 확인).
STRENGTH_MIN_TICKS = 5      # 윈도우 내 틱 수가 이 미만이면 판단 불가 -> 중립값
STRENGTH_NEUTRAL = 100.0    # 판단 불가 시 반환값(매수:매도 1:1과 동일 의미)
STRENGTH_CAP = 300.0        # 계산되더라도 이 값으로 상한(그 이상은 다 "매수 우세"로 동일 취급)


class TradeFlowTracker:
    """체결 슬라이딩 윈도우 + 시간가중 강도 + 가격 추적."""

    def __init__(self, max_window_sec: float = 120):
        # {code: deque[(ts, price, side, volume)]}
        self.ticks: dict[str, deque] = {}
        self.max_window_sec = max_window_sec

    def add_tick(
        self,
        stock_code: str,
        price: int,
        side: str,
        volume: int,
        now: float = None,
    ):
        """0B 체결 1건 추가. side: 'buy'/'sell'/'neutral'."""
        now = now if now is not None else time.time()
        d = self.ticks.setdefault(stock_code, deque())
        d.append((now, price, side, volume))

        # 오래된 데이터 제거
        cutoff = now - self.max_window_sec
        while d and d[0][0] < cutoff:
            d.popleft()

    def compute_strength(
        self,
        stock_code: str,
        window_sec: float,
        now: float = None,
        weight_fn: Callable[[float, float], float] = None,
        min_ticks: int = None,
    ) -> float:
        """
        시간가중 체결강도 = 가중 매수체결량 / 가중 매도체결량 × 100.

        weight_fn(age_sec, window_sec) → 0~1. 기본은 선형 감쇠.

        노이즈 방지(2026-07-29): 윈도우 내 틱 수가 STRENGTH_MIN_TICKS 미만이면
        판단하기엔 데이터가 너무 적다고 보고 STRENGTH_NEUTRAL(100.0) 반환 —
        매도가 우연히 0~1건이라 9999 같은 값이 나오는 걸 원천 차단. 데이터가
        충분해도 결과는 STRENGTH_CAP으로 상한(매수 우세라는 의미는 200이나
        9999나 같아서 세분화할 근거가 없고, 극단값일수록 실전에서 노이즈였을
        확률이 높았음).

        min_ticks: 기본값(STRENGTH_MIN_TICKS=5)을 호출부가 덮어쓸 수 있게 한 것
        (2026-08-01 신규). 1A가 3초짜리 짧은 윈도우로 판정하게 바뀌면서 5틱은
        과도한 요구가 됐다 — 다만 호출부는 반환값이 중립값인지(=판단 불가)
        틱 수로 직접 확인해야 한다(tick_count() 참고).
        """
        now = now if now is not None else time.time()
        d = self.ticks.get(stock_code)
        if not d:
            return 0.0

        weight_fn = weight_fn or self.linear_weight
        cutoff = now - window_sec
        w_buy = 0.0
        w_sell = 0.0
        tick_count = 0
        for ts, price, side, vol in d:
            if ts < cutoff or ts > now:
                continue
            tick_count += 1
            w = weight_fn(now - ts, window_sec)
            if side == "buy":
                w_buy += vol * w
            elif side == "sell":
                w_sell += vol * w

        if tick_count < (STRENGTH_MIN_TICKS if min_ticks is None else min_ticks):
            return STRENGTH_NEUTRAL

        if w_sell == 0:
            return STRENGTH_CAP if w_buy > 0 else 0.0
        return min(w_buy / w_sell * 100, STRENGTH_CAP)

    def is_strength_rising(
        self,
        stock_code: str,
        short_window: float = 10,
        long_window: float = 30,
        now: float = None,
        min_short_strength: float = 0,
    ) -> bool:
        """단기 윈도우 강도가 장기 윈도우 강도보다 강한지."""
        s_short = self.compute_strength(stock_code, short_window, now)
        s_long = self.compute_strength(stock_code, long_window, now)
        return s_short > s_long and s_short >= min_short_strength

    def get_stats(
        self, stock_code: str, window_sec: float = 30, now: float = None
    ) -> dict:
        """디버그/관찰용 통계."""
        now = now if now is not None else time.time()
        d = self.ticks.get(stock_code, deque())
        cutoff = now - window_sec
        recent = [t for t in d if t[0] >= cutoff]
        buy_vol = sum(v for _, _, s, v in recent if s == "buy")
        sell_vol = sum(v for _, _, s, v in recent if s == "sell")
        if len(recent) < STRENGTH_MIN_TICKS:
            simple = STRENGTH_NEUTRAL
        elif sell_vol:
            simple = min(buy_vol / sell_vol * 100, STRENGTH_CAP)
        else:
            simple = STRENGTH_CAP if buy_vol else 0.0
        return {
            "count": len(recent),
            "buy_vol": buy_vol,
            "sell_vol": sell_vol,
            "strength_simple": simple,
            "strength_weighted": self.compute_strength(stock_code, window_sec, now),
        }

    # ─── 가격 추적 ─────────────────────────────────
    def get_latest_price(self, stock_code: str) -> Optional[int]:
        """가장 최근 체결가."""
        d = self.ticks.get(stock_code)
        if not d:
            return None
        return d[-1][1]

    def get_price_around(
        self,
        stock_code: str,
        seconds_ago: float,
        now: float = None,
    ) -> Optional[int]:
        """N초 전 가격 = 그 시점 이전의 가장 최근 체결가.

        해당 시점보다 과거 데이터가 없으면 None.
        """
        now = now if now is not None else time.time()
        target = now - seconds_ago
        d = self.ticks.get(stock_code)
        if not d:
            return None
        # 최근 → 과거로 walking. target 이하의 첫 틱 = N초 전 가격
        for ts, price, side, vol in reversed(d):
            if ts <= target:
                return price
        return None

    def get_price_change_pct(
        self,
        stock_code: str,
        seconds_ago: float = 60,
        now: float = None,
    ) -> Optional[float]:
        """N초 전 대비 현재가 변화율 (%). 양수=상승, 음수=하락. 눌림 감지용."""
        cur = self.get_latest_price(stock_code)
        past = self.get_price_around(stock_code, seconds_ago, now)
        if cur is None or past is None or past == 0:
            return None
        return (cur - past) / past * 100

    def get_trade_value(
        self,
        stock_code: str,
        window_sec: float,
        now: float = None,
    ) -> float:
        """최근 window_sec 동안의 누적 거래대금(가격*수량 합, 원).

        체결강도(compute_strength)와 같은 틱 버퍼를 그대로 재사용 — REST
        호출도, 추가 대기시간도 없다(2026-07-31, 유동성 필터용 신규).
        데이터 없으면 0.0(호출부가 "유동성 확인 불가"로 보수적으로 처리)."""
        now = now if now is not None else time.time()
        d = self.ticks.get(stock_code)
        if not d:
            return 0.0
        cutoff = now - window_sec
        return sum(price * volume for ts, price, side, volume in d if ts >= cutoff)

    def avg_trade_value(
        self,
        stock_code: str,
        window_sec: float,
        now: float = None,
        min_ticks: int = 20,
        exclude_recent_sec: float = 0.0,
    ) -> float:
        """구간 내 **1틱당 평균 체결금액**(원). 없으면 0.0.

        exclude_recent_sec: 최근 이 시간(초)만큼을 평균에서 **제외**한다.
        상대 버스트 판정의 분모로 쓸 때 반드시 필요하다 — 지금 터지고 있는
        대량체결이 평균에 같이 들어가면 그 체결이 클수록 문턱도 같이 올라가서
        (분자와 분모가 함께 커짐) 정작 큰 버스트일수록 통과가 어려워지는
        자기모순이 생긴다. 실제로 이 값이 0일 때 격리 테스트에서
        "평균의 40배짜리 체결 2건"이 탈락했다.
        즉 여기서 재는 건 '버스트 직전까지의 평상시 체결 크기'다.

        (2026-08-02 신규) 대량체결 버스트의 '상대 경로' 판정용 —
        "단일 체결이 이 종목 평소 체결의 몇 배인가"를 재는 분모다.

        절대금액(3천만원)만 쓰면 종목마다 난이도가 다르다: 조건검색식이
        보장하는 건 일 거래대금 100억 이상뿐이라 평균 1틱 금액이 30만원인
        종목과 300만원인 종목이 같은 풀에 섞인다(10배 차이). 전자에게
        3천만원은 평균의 100배(사실상 불가능)지만 후자에겐 10배다.

        시간대 보정이 자동으로 되는 것도 이 지표의 핵심 성질이다 — 점심에
        시장 전체가 마르면 분모(평균 1틱)도 같이 줄어들어 '평균의 N배'라는
        난이도가 시각과 무관하게 유지된다. 별도 시간계수가 필요 없다.

        min_ticks: 표본이 너무 적으면 평균이 우연에 휘둘리므로(체결 3건 중
        하나가 큰 건이면 평균이 통째로 끌려올라가 배수 판정이 무력화된다)
        미달 시 0.0을 반환한다 — 호출부는 이걸 '상대 경로 판단 불가'로 보고
        절대 경로만 쓴다.
        """
        now = now if now is not None else time.time()
        d = self.ticks.get(stock_code)
        if not d:
            return 0.0
        cutoff = now - window_sec
        upper = now - max(0.0, exclude_recent_sec)
        vals = [
            price * volume for ts, price, _, volume in d
            if cutoff <= ts <= upper
        ]
        if len(vals) < min_ticks:
            return 0.0
        return float(sum(vals)) / len(vals)

    def tick_count(self, stock_code: str, window_sec: float, now: float = None) -> int:
        """최근 window_sec 안의 체결 틱 수 (2026-08-01 신규).

        compute_strength가 중립값(100.0)을 돌려줬을 때 그게 '진짜 1:1'인지
        '틱이 부족해 판단 불가'인지 호출부가 구분할 수 있어야 해서 추가했다 —
        이 구분이 없어서 07-31에 보유종목이 매수 66초 만에 잘려나간 사고가 있었다.
        """
        now = now if now is not None else time.time()
        d = self.ticks.get(stock_code)
        if not d:
            return 0
        cutoff = now - window_sec
        return sum(1 for ts, _, _, _ in d if ts >= cutoff)

    def value_acceleration(
        self,
        stock_code: str,
        short_sec: float = 30,
        long_sec: float = 120,
        now: float = None,
    ) -> float:
        """거래대금 '가속도' — 최근 short_sec 거래대금이 long_sec 평균 대비 몇 배인가.

        (2026-08-01 신규, 종목 우선순위용) 1.0 = 균일하게 들어옴, 3.0 = 최근
        구간에 3배로 몰림. **자기 자신 대비 비율**이라 대형주/소형주가 같은
        잣대로 비교된다 — 절대 거래대금으로 줄을 세우면 삼성전자가 항상 이기고
        조건검색이 굳이 찾아준 소형 급등주가 영영 슬롯을 못 잡는다.

        같은 틱 버퍼만 읽으므로 REST 0콜, 대기 0초 — 트리거 시점에 호출해도
        진입이 늦어지지 않는다(우선순위 때문에 급등 포착이 느려지면 안 되므로
        이 성질이 설계의 핵심이다).

        데이터가 부족하면 0.0 — 호출부는 이걸 '순위 판단 불가'로 보고
        **교체(남의 슬롯 빼앗기)만 포기**하고, 빈 슬롯 매수는 그대로 진행한다.
        """
        now = now if now is not None else time.time()
        if long_sec <= short_sec or short_sec <= 0:
            return 0.0
        long_val = self.get_trade_value(stock_code, long_sec, now=now)
        if long_val <= 0:
            return 0.0
        short_val = self.get_trade_value(stock_code, short_sec, now=now)
        expected = long_val * (short_sec / long_sec)   # 균일하게 들어왔다면 이만큼
        if expected <= 0:
            return 0.0
        return short_val / expected

    def get_low(
        self,
        stock_code: str,
        window_sec: float,
        now: float = None,
    ) -> Optional[float]:
        """최근 window_sec 안의 **최저 체결가**. 틱이 없으면 None.

        (2026-08-05 신규) 손절 대신 추가매수(Rescue Add)의 '반등 확증' 판정용.
        포지션의 `lowest_price`는 매수 이후 전 구간의 저점이라 "지금 막 반등을
        시작했는가"를 못 잰다 — 그건 짧은 창의 저점이어야 한다.
        None은 '판단 불가'이며, 호출부는 이를 **추가매수 미발동**(=손절)으로
        보수적으로 처리한다.
        """
        now = now if now is not None else time.time()
        d = self.ticks.get(stock_code)
        if not d:
            return None
        lows = [px for ts, px, _side, _vol in d if now - ts <= window_sec and px > 0]
        return float(min(lows)) if lows else None

    def count_large_trades(
        self,
        stock_code: str,
        window_sec: float,
        min_value: float,
        now: float = None,
        side: str = None,
    ) -> int:
        """최근 window_sec 안에서 '단일 체결 금액 >= min_value'인 체결 건수.

        (2026-08-01 신규, 사용자 지정 1A 진입조건) 누적 거래대금은 잔챙이
        체결이 길게 쌓여도 채워지지만, 이건 '큰 손이 지금 때리고 있는가'를
        본다 — 같은 틱 버퍼를 재사용하므로 REST 호출도 대기시간도 없다.

        side (2026-08-06 신규): 지정하면 그 방향의 체결만 센다.
          **기본값 None은 방향 무관 = 기존 동작**이라 다른 호출부가 조용히
          바뀌지 않는다. 진입 버스트 판정만 `"buy"`를 넘긴다.

          왜 필요한가 — 예전엔 매도 체결도 버스트로 셌다. 무장에 쓰는
          FID 228 체결강도는 **당일 누적**이라 오전에 강했던 종목은 급락
          중에도 100 위에 머문다. 그래서 "오전에 강했던 종목이 대량 투매로
          무너지는 순간"이 그대로 매수 신호가 됐다(08-06 손절 7건의 구조적 원인).
        """
        now = now if now is not None else time.time()
        d = self.ticks.get(stock_code)
        if not d:
            return 0
        cutoff = now - window_sec
        return sum(
            1 for ts, price, tick_side, volume in d
            if ts >= cutoff and price * volume >= min_value
            and (side is None or tick_side == side)
        )

    def max_single_trade_value(
        self,
        stock_code: str,
        window_sec: float,
        now: float = None,
        side: str = None,
    ) -> float:
        """최근 window_sec 안의 최대 단일 체결 금액(원). 없으면 0.0.

        (2026-08-01 신규) '단일 체결 1억원 이상' 조건 판정용.
        side는 count_large_trades와 같은 규약 — 기본 None이면 방향 무관(기존 동작).
        """
        now = now if now is not None else time.time()
        d = self.ticks.get(stock_code)
        if not d:
            return 0.0
        cutoff = now - window_sec
        vals = [
            price * volume for ts, price, tick_side, volume in d
            if ts >= cutoff and (side is None or tick_side == side)
        ]
        return float(max(vals)) if vals else 0.0
    # ──────────────────────────────────────────

    def reset(self, stock_code: str):
        self.ticks.pop(stock_code, None)

    # ─── 가중치 함수들 ──────────────────────────────
    @staticmethod
    def linear_weight(age: float, window: float) -> float:
        """선형 감쇠: age=0 → 1.0, age=window → 0.0."""
        if window <= 0:
            return 0.0
        return max(0.0, 1.0 - age / window)

    @staticmethod
    def exponential_weight(tau: float) -> Callable[[float, float], float]:
        """지수 감쇠 factory. tau가 작을수록 최근 가중치 ↑."""
        def fn(age: float, window: float) -> float:
            return math.exp(-age / tau)
        return fn
    def is_intensity_sustained(
        self,
        stock_code: str,
        threshold: float,
        duration_sec: int,
        now: float = None,
        sample_window_sec: float = 10,
        min_ticks: int = 5,
    ) -> bool:
        """특정 체결강도(threshold) 이상이 duration_sec 동안 끊기지 않고 유지됐는지 확인.
        duration_sec 구간을 sample_window_sec 간격으로 샘플링하며, 각 샘플 시점의
        시간가중 체결강도(compute_strength)가 전부 threshold 이상이어야 True.
        한 구간이라도 threshold 밑으로 떨어지면(또는 틱이 비어 0으로 계산되면) False."""
        now = now if now is not None else time.time()
        d = self.ticks.get(stock_code)
        if not d:
            return False

        cutoff = now - duration_sec
        recent = [t for t in d if t[0] >= cutoff]
        if len(recent) < min_ticks:
            return False

        step = max(sample_window_sec, 1)
        sample_time = now
        end_time = now - duration_sec
        while sample_time >= end_time:
            strength = self.compute_strength(stock_code, sample_window_sec, now=sample_time)
            if strength < threshold:
                return False
            sample_time -= step

        return True