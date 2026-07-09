"""
어제 09:21~10:40 (실제 Phase2 구간)에서 매 분봉마다 진입조건을 평가.

분봉은 최신순([0]=최신)이므로, idx번째 봉을 '그 시점의 현재봉'으로 보려면
candles[idx:] 슬라이스가 그 시점 기준 과거 데이터가 된다.
→ 각 시점마다 evaluate_phase2 와 동일한 계산을 재현해서
   "그 80분 동안 종목별로 몇 번 전체조건을 만족했나"를 센다.

실행: python test_phase2_backtest.py
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

# 평가할 시간대 (HHMM, 포함)
WINDOW_START = "0921"
WINDOW_END = "1040"

# 어제 09:21까지 닿으려면 충분히 크게 (15:35 - 09:21 ≈ 374분 + 60MA 여유)
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
    """time_str(YYYYMMDDHHMMSS) → HHMM."""
    return candle["time_str"][8:12]


def evaluate_at(candles, idx, tolerance):
    """
    candles[idx]를 현재봉으로 보고 Phase2 조건 평가.
    슬라이스 candles[idx:] 가 그 시점 과거 데이터.
    반환: (touch, bullish, vol_ok, full, diff_pct, vol_ratio) 또는 None(분봉부족)
    """
    sub = candles[idx:]
    if len(sub) < 60:
        return None
    cur = sub[0]
    ma60 = calc_ma(sub, 60)
    if not ma60:
        return None
    price = cur["close"]
    touch = is_ma_touch(price, ma60, tolerance)
    bullish = is_bullish_candle(cur)
    vol_ratio = get_volume_ratio(sub, VOLUME_LOOKBACK)
    vol_ok = vol_ratio >= VOLUME_SURGE_RATIO
    diff_pct = (price - ma60) / ma60 * 100
    full = touch and bullish and vol_ok
    return (touch, bullish, vol_ok, full, diff_pct, vol_ratio)


def main():
    token = get_access_token()
    if not token:
        print("❌ 토큰 발급 실패")
        return
    print(f"✅ 토큰 발급 완료: {token[:20]}...")

    rest = KiwoomREST(token, is_mock=settings.IS_MOCK)

    print(f"\n평가 시간대: {WINDOW_START[:2]}:{WINDOW_START[2:]} ~ "
          f"{WINDOW_END[:2]}:{WINDOW_END[2:]}  (분봉 {FETCH_COUNT}개 요청)\n")

    # 종목별 결과: 각 톨러런스마다 전체조건 만족 분(分) 수
    summary = []

    for code, name in TARGETS:
        candles = rest.get_minute_candles(code, interval=1, count=FETCH_COUNT)
        if not candles:
            print(f"  {name} ({code}): 데이터 없음")
            continue

        # 시간대에 해당하는 인덱스 추출
        window_idx = [
            i for i, c in enumerate(candles)
            if WINDOW_START <= hhmm_of(c) <= WINDOW_END
        ]
        if not window_idx:
            print(f"  {name} ({code}): 해당 시간대 봉 없음 "
                  f"(받은 범위 [0]={candles[0]['time_str']} ~ [-1]={candles[-1]['time_str']})")
            continue

        # 각 톨러런스별 카운트
        cnt_full = {t: 0 for t in TOLERANCE_CANDIDATES}
        cnt_touch = {t: 0 for t in TOLERANCE_CANDIDATES}
        cnt_bullish = 0
        cnt_volok = 0
        n_eval = 0
        best_vol = 0.0

        for i in window_idx:
            r = evaluate_at(candles, i, TOLERANCE_CANDIDATES[0])
            if r is None:
                continue
            n_eval += 1
            _, bullish, vol_ok, _, _, vol_ratio = r
            if bullish:
                cnt_bullish += 1
            if vol_ok:
                cnt_volok += 1
            best_vol = max(best_vol, vol_ratio)
            for t in TOLERANCE_CANDIDATES:
                touch, b, v, full, _, _ = evaluate_at(candles, i, t)
                if touch:
                    cnt_touch[t] += 1
                if full:
                    cnt_full[t] += 1

        summary.append({
            "name": name, "code": code, "n_eval": n_eval,
            "cnt_full": cnt_full, "cnt_touch": cnt_touch,
            "cnt_bullish": cnt_bullish, "cnt_volok": cnt_volok,
            "best_vol": best_vol,
            "range": (candles[0]["time_str"], candles[-1]["time_str"]),
        })

    # ── 결과 표 ──
    print("\n" + "=" * 84)
    print("어제 09:21~10:40 구간 — 종목별 조건 충족 '분' 수 (분모=평가 가능 분 수)")
    print("=" * 84)
    print(f"{'종목':<14}{'평가분':>6}{'양봉':>6}{'거래≥1.5x':>10}"
          f"{'터치±0.3':>9}{'터치±0.5':>9}{'터치±1':>8}"
          f"{'전체±0.3':>9}{'전체±0.5':>9}{'전체±1':>8}")
    print("-" * 84)
    for s in summary:
        print(f"{s['name']:<14}{s['n_eval']:>6}{s['cnt_bullish']:>6}{s['cnt_volok']:>10}"
              f"{s['cnt_touch'][0.3]:>9}{s['cnt_touch'][0.5]:>9}{s['cnt_touch'][1.0]:>8}"
              f"{s['cnt_full'][0.3]:>9}{s['cnt_full'][0.5]:>9}{s['cnt_full'][1.0]:>8}")

    print("-" * 84)
    # 합계
    tot = {t: sum(s["cnt_full"][t] for s in summary) for t in TOLERANCE_CANDIDATES}
    print(f"{'전체조건 합계':<14}", end="")
    print(f"{'':>22}{'':>26}", end="")
    print(f"{tot[0.3]:>9}{tot[0.5]:>9}{tot[1.0]:>8}")

    print("\n해석:")
    print(" - '전체±t'가 0이면, 그 시간대에 Phase2 매수 신호가 한 번도 안 떴다는 뜻.")
    print(" - 양봉/거래량 칸이 낮으면 그게 병목. 터치 칸이 낮으면 60MA가 병목.")
    print(" - '거래≥1.5x'가 0이면 거래량 1.5배 조건이 그 종목·시간대에선 사실상 불가.")
    print("\n✅ 완료")


if __name__ == "__main__":
    main()