"""
어제 09:21~10:40 구간에서 30MA vs 60MA 신호 빈도 비교.

같은 구간·같은 톨러런스에서 MA 기간만 30/60으로 바꿔
전체조건(MA터치 AND 양봉 AND 거래량1.5x) 충족 '분' 수를 나란히 센다.

실행: python test_ma_compare.py
"""
from api.auth import get_access_token
from api.kiwoom_rest import KiwoomREST
from config import settings

from core.strategy.indicators import (
    calc_ma,
    is_ma_touch,
    is_bullish_candle,
    get_volume_ratio,
)

VOLUME_SURGE_RATIO = 1.5
VOLUME_LOOKBACK = 5
TOLERANCE_CANDIDATES = [0.3, 0.5, 1.0]
MA_PERIODS = [30, 60]

WINDOW_START = "0921"
WINDOW_END = "1040"
FETCH_COUNT = 450

TARGETS = [
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("042700", "한미반도체"),
    ("035720", "카카오"),
    ("035420", "NAVER"),
    ("247540", "에코프로비엠"),
    ("196170", "알테오젠"),
    ("373220", "LG에너지솔루션"),
]


def hhmm_of(candle):
    return candle["time_str"][8:12]


def eval_full(candles, idx, period, tolerance):
    """candles[idx]를 현재봉으로 보고 (MA period, tolerance)에서 전체조건 만족?"""
    sub = candles[idx:]
    if len(sub) < period:
        return None
    cur = sub[0]
    ma = calc_ma(sub, period)
    if not ma:
        return None
    touch = is_ma_touch(cur["close"], ma, tolerance)
    bullish = is_bullish_candle(cur)
    vol_ok = get_volume_ratio(sub, VOLUME_LOOKBACK) >= VOLUME_SURGE_RATIO
    return bool(touch and bullish and vol_ok), bool(touch)


def main():
    token = get_access_token()
    if not token:
        print("❌ 토큰 발급 실패")
        return
    print(f"✅ 토큰 발급 완료: {token[:20]}...\n")

    rest = KiwoomREST(token, is_mock=settings.IS_MOCK)

    results = []
    for code, name in TARGETS:
        candles = rest.get_minute_candles(code, interval=1, count=FETCH_COUNT)
        if not candles:
            continue
        window_idx = [i for i, c in enumerate(candles)
                      if WINDOW_START <= hhmm_of(c) <= WINDOW_END]
        if not window_idx:
            continue

        # full[period][tol] = 분 수
        full = {p: {t: 0 for t in TOLERANCE_CANDIDATES} for p in MA_PERIODS}
        n_eval = 0
        for i in window_idx:
            counted = False
            for p in MA_PERIODS:
                for t in TOLERANCE_CANDIDATES:
                    r = eval_full(candles, i, p, t)
                    if r is None:
                        continue
                    counted = True
                    if r[0]:
                        full[p][t] += 1
            if counted:
                n_eval += 1
        results.append({"name": name, "n_eval": n_eval, "full": full})

    # ── 표: 종목별 30MA vs 60MA 전체조건 분 수 ──
    print("=" * 88)
    print("전체조건(터치 AND 양봉 AND 거래량1.5x) 충족 분 수 — 30MA vs 60MA")
    print("=" * 88)
    print(f"{'종목':<14}{'평가':>5}"
          f"{'30±0.3':>8}{'30±0.5':>8}{'30±1':>7}"
          f"{'60±0.3':>8}{'60±0.5':>8}{'60±1':>7}")
    print("-" * 88)
    totals = {p: {t: 0 for t in TOLERANCE_CANDIDATES} for p in MA_PERIODS}
    for r in results:
        f = r["full"]
        print(f"{r['name']:<14}{r['n_eval']:>5}"
              f"{f[30][0.3]:>8}{f[30][0.5]:>8}{f[30][1.0]:>7}"
              f"{f[60][0.3]:>8}{f[60][0.5]:>8}{f[60][1.0]:>7}")
        for p in MA_PERIODS:
            for t in TOLERANCE_CANDIDATES:
                totals[p][t] += f[p][t]
    print("-" * 88)
    print(f"{'합계':<14}{'':>5}"
          f"{totals[30][0.3]:>8}{totals[30][0.5]:>8}{totals[30][1.0]:>7}"
          f"{totals[60][0.3]:>8}{totals[60][0.5]:>8}{totals[60][1.0]:>7}")

    print("\n해석:")
    print(" - 30MA가 60MA보다 신호 분 수가 크면 '더 잘 잡힌다'는 뜻.")
    print(" - 단, 신호가 많다고 좋은 게 아니라 노이즈성 진입도 늘어남(질 저하).")
    print(" - 같은 톨러런스끼리 30 vs 60 비교가 핵심.")
    print("\n✅ 완료")


if __name__ == "__main__":
    main()