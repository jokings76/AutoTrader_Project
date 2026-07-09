"""
indicators.py 단독 테스트.
실제 분봉 데이터로 본인 전략의 매수 조건 검증.

실행: python test_indicators.py
"""
from datetime import datetime

from api.auth import get_access_token
from api.kiwoom_rest import KiwoomREST
from config import settings
from core.strategy.indicators import (
    calc_ma,
    is_ma_touch,
    is_volume_breakout,
    get_volume_ratio,
    is_bullish_candle,
    get_today_first_candle,
    calc_rise_pct_from_open,
)


def evaluate_phase1(candles: list[dict], target_date: str) -> tuple[bool, dict]:
    """
    Phase 1: 5MA 터치 매수 조건 평가.
    
    조건:
    1. 시초가 대비 +5% 이상 급등
    2. 5MA 터치 (±0.3%)
    3. 양봉
    4. 거래량 1.5배 이상
    """
    info = {}
    
    if len(candles) < 6:
        return False, {"reason": f"1분봉 부족 ({len(candles)}개)"}
    
    latest = candles[0]
    current = latest["close"]
    info["current"] = current
    
    # 조건 1: 급등 상태 (+5% 이상)
    today_first = get_today_first_candle(candles, target_date)
    if today_first:
        rise_pct = calc_rise_pct_from_open(current, today_first["open"])
        info["rise_pct"] = round(rise_pct, 2)
        if rise_pct < 5:
            return False, {**info, "reason": f"급등 미확인 ({rise_pct:+.2f}%)"}
    else:
        info["rise_pct"] = None  # 오늘 데이터 없음
    
    # 조건 2: 5MA 터치
    ma5 = calc_ma(candles, 5)
    info["ma5"] = round(ma5, 0)
    if ma5 == 0:
        return False, {**info, "reason": "5MA 계산 불가"}
    
    diff5 = (current - ma5) / ma5 * 100
    info["ma5_diff_pct"] = round(diff5, 2)
    
    if not is_ma_touch(current, ma5, 0.3):
        return False, {**info, "reason": f"5MA 미터치 ({diff5:+.2f}%)"}
    
    # 조건 3: 양봉
    if not is_bullish_candle(latest):
        return False, {**info, "reason": "음봉 (반등 미확인)"}
    
    # 조건 4: 거래량 1.5배
    vol_ratio = get_volume_ratio(candles, 5)
    info["volume_ratio"] = round(vol_ratio, 2)
    if not is_volume_breakout(candles, 1.5):
        return False, {**info, "reason": f"거래량 부족 ({vol_ratio:.2f}배)"}
    
    return True, {**info, "reason": "모든 조건 충족!"}


def evaluate_phase2(candles: list[dict]) -> tuple[bool, dict]:
    """
    Phase 2: 60MA 터치 매수 조건 평가.
    """
    info = {}
    
    if len(candles) < 60:
        return False, {"reason": f"1분봉 부족 ({len(candles)}개, 60개 필요)"}
    
    latest = candles[0]
    current = latest["close"]
    info["current"] = current
    
    # 조건 1: 60MA 터치
    ma60 = calc_ma(candles, 60)
    info["ma60"] = round(ma60, 0)
    if ma60 == 0:
        return False, {**info, "reason": "60MA 계산 불가"}
    
    diff60 = (current - ma60) / ma60 * 100
    info["ma60_diff_pct"] = round(diff60, 2)
    
    if not is_ma_touch(current, ma60, 0.3):
        return False, {**info, "reason": f"60MA 미터치 ({diff60:+.2f}%)"}
    
    # 조건 2: 양봉
    if not is_bullish_candle(latest):
        return False, {**info, "reason": "음봉"}
    
    # 조건 3: 거래량
    vol_ratio = get_volume_ratio(candles, 5)
    info["volume_ratio"] = round(vol_ratio, 2)
    if not is_volume_breakout(candles, 1.5):
        return False, {**info, "reason": f"거래량 부족 ({vol_ratio:.2f}배)"}
    
    return True, {**info, "reason": "모든 조건 충족!"}


def main():
    token = get_access_token()
    if not token:
        print("❌ 토큰 발급 실패")
        return
    print(f"✅ 토큰 발급 완료\n")
    
    rest = KiwoomREST(token, is_mock=settings.IS_MOCK)
    
    # 테스트할 종목들 (단타 인기 종목 위주)
    targets = [
        ("005930", "삼성전자"),
        ("000660", "SK하이닉스"),
        ("042700", "한미반도체"),
    ]
    
    # 마지막 거래일 (5/8 금요일)
    target_date = "20260508"
    
    print("=" * 70)
    print(f"본인 전략 매수 조건 시뮬레이션 (대상일: {target_date})")
    print("=" * 70)
    print(f"\n조건 정리:")
    print(f"  Phase 1: 시초가+5% & 5MA터치(±0.3%) & 양봉 & 거래량 1.5배")
    print(f"  Phase 2: 60MA터치(±0.3%) & 양봉 & 거래량 1.5배")
    
    for code, name in targets:
        print(f"\n{'='*70}")
        print(f"  {name} ({code})")
        print(f"{'='*70}")
        
        # 1분봉 70개 가져오기
        candles = rest.get_minute_candles(code, interval=1, count=70, base_date=target_date)
        
        if not candles:
            print(f"  ⚠️ 분봉 데이터 없음")
            continue
        
        latest = candles[0]
        print(f"  최근 봉: {latest['time_str']}")
        print(f"  현재가:   {latest['close']:>10,}원")
        
        ma5 = calc_ma(candles, 5) if len(candles) >= 5 else 0
        ma60 = calc_ma(candles, 60) if len(candles) >= 60 else 0
        if ma5:
            print(f"  5MA:    {ma5:>10,.0f}원  (대비 {(latest['close']-ma5)/ma5*100:+.2f}%)")
        if ma60:
            print(f"  60MA:   {ma60:>10,.0f}원  (대비 {(latest['close']-ma60)/ma60*100:+.2f}%)")
        
        vol_ratio = get_volume_ratio(candles, 5)
        print(f"  거래량비: {vol_ratio:.2f}배 (직전 5봉 평균 대비)")
        
        # ───── Phase 1 평가 ─────
        print(f"\n  [Phase 1 평가]")
        passed, info = evaluate_phase1(candles, target_date)
        result = "✅ 매수 트리거!" if passed else "❌ 매수 안 함"
        print(f"  {result}: {info.get('reason')}")
        
        # ───── Phase 2 평가 ─────
        print(f"\n  [Phase 2 평가]")
        passed, info = evaluate_phase2(candles)
        result = "✅ 매수 트리거!" if passed else "❌ 매수 안 함"
        print(f"  {result}: {info.get('reason')}")
    
    print(f"\n{'='*70}")
    print("✅ 시뮬레이션 완료")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()