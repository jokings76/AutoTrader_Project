"""
Phase2 진입조건(60MA 터치 + 양봉 + 거래량 1.5배) 민감도 점검.

봇과 100% 동일한 계산을 위해 core.strategy.indicators 를 그대로 import.
톨러런스를 여러 값으로 바꿔가며 종목별 통과 여부를 표로 출력한다.

실행: python test_phase2_tolerance.py
주의: 장 시작 전에 돌리면 직전 거래일 막판 시점 기준 평가가 된다.
      (실제 Phase2 구간 09:21~10:40 재현은 아님 — 톨러런스 민감도 파악용)
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

# strategy_manager.py 의 Phase2 상수와 동일
VOLUME_SURGE_RATIO = 1.5
VOLUME_LOOKBACK = 5
MA_TOUCH_TOLERANCE_PCT = 0.3   # 기본값 (±0.3%)

# 비교할 톨러런스 후보 (%)
TOLERANCE_CANDIDATES = [0.3, 0.5, 1.0, 2.0]

# 점검할 종목 (code, name) — 필요하면 자유롭게 추가
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


def evaluate_phase2_components(candles):
    """
    Phase2 조건을 구성요소별로 분해해서 반환.
    실제 evaluate_phase2 로직과 동일한 계산을 사용하되,
    터치 판정은 톨러런스별로 따로 계산할 수 있게 raw 값을 돌려준다.
    """
    if len(candles) < 60:
        return {"enough": False, "n": len(candles)}

    cur = candles[0]
    current_price = cur["close"]
    ma60 = calc_ma(candles, 60)
    if not ma60:
        return {"enough": False, "n": len(candles)}

    diff_pct = (current_price - ma60) / ma60 * 100
    bullish = is_bullish_candle(cur)
    vol_ratio = get_volume_ratio(candles, VOLUME_LOOKBACK)

    return {
        "enough": True,
        "n": len(candles),
        "current_price": current_price,
        "ma60": ma60,
        "diff_pct": diff_pct,
        "bullish": bullish,
        "vol_ratio": vol_ratio,
        "vol_ok": vol_ratio >= VOLUME_SURGE_RATIO,
    }


def main():
    token = get_access_token()
    if not token:
        print("❌ 토큰 발급 실패")
        return
    print(f"✅ 토큰 발급 완료: {token[:20]}...")

    rest = KiwoomREST(token, is_mock=settings.IS_MOCK)

    rows = []
    for code, name in TARGETS:
        candles = rest.get_minute_candles(code, interval=1, count=70)
        comp = evaluate_phase2_components(candles)
        comp["code"] = code
        comp["name"] = name
        rows.append(comp)

    # ── 구성요소 표 ──
    print("\n" + "=" * 78)
    print("Phase2 구성요소 진단 (현재가 / 60MA 대비 / 양봉 / 거래량비)")
    print("=" * 78)
    print(f"{'종목':<14}{'현재가':>10}{'60MA대비':>11}{'양봉':>6}{'거래량비':>10}")
    print("-" * 78)
    for r in rows:
        if not r.get("enough"):
            print(f"{r['name']:<14}  분봉 부족 ({r.get('n', 0)}개) — 60MA 계산 불가")
            continue
        print(f"{r['name']:<14}"
              f"{r['current_price']:>10,}"
              f"{r['diff_pct']:>+10.2f}%"
              f"{'O' if r['bullish'] else 'X':>6}"
              f"{r['vol_ratio']:>9.2f}x")

    # ── 톨러런스별 통과 집계 ──
    print("\n" + "=" * 78)
    print("톨러런스별 Phase2 전체조건 통과 (터치 AND 양봉 AND 거래량≥1.5x)")
    print("=" * 78)
    header = f"{'종목':<14}"
    for t in TOLERANCE_CANDIDATES:
        header += f"±{t}%".rjust(9)
    print(header)
    print("-" * 78)

    pass_count = {t: 0 for t in TOLERANCE_CANDIDATES}
    touch_count = {t: 0 for t in TOLERANCE_CANDIDATES}

    for r in rows:
        line = f"{r['name']:<14}"
        if not r.get("enough"):
            for t in TOLERANCE_CANDIDATES:
                line += "부족".rjust(8)
            print(line)
            continue
        for t in TOLERANCE_CANDIDATES:
            touch = is_ma_touch(r["current_price"], r["ma60"], t)
            if touch:
                touch_count[t] += 1
            full = touch and r["bullish"] and r["vol_ok"]
            if full:
                pass_count[t] += 1
            # 전체통과 O / 터치만 t / 전부탈락 X
            mark = "O" if full else ("t" if touch else "X")
            line += mark.rjust(9)
        print(line)

    print("-" * 78)
    n_valid = sum(1 for r in rows if r.get("enough"))
    print(f"{'터치만 통과':<14}", end="")
    for t in TOLERANCE_CANDIDATES:
        print(f"{touch_count[t]}/{n_valid}".rjust(9), end="")
    print()
    print(f"{'전체조건 통과':<14}", end="")
    for t in TOLERANCE_CANDIDATES:
        print(f"{pass_count[t]}/{n_valid}".rjust(9), end="")
    print()

    print("\n범례: O=전체조건 통과 / t=60MA 터치는 했으나 양봉·거래량에서 탈락 / X=터치 실패")
    print("※ 장 시작 전 실행 시 직전 거래일 막판 기준 — 톨러런스 민감도 파악용")
    print("\n✅ 완료")


if __name__ == "__main__":
    main()