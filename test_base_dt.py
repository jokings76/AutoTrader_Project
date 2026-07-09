"""
ka10080 base_dt 파라미터에 따라 응답 분봉 개수가 어떻게 달라지는지 진단.

장중 1개만 오는 문제 원인 규명용.
같은 종목을 base_dt 3가지 방식으로 호출해서 raw 응답 row 개수를 비교한다.

실행: python test_base_dt.py 005930
"""
import sys
import json
from datetime import datetime

from api.auth import get_access_token
from api.kiwoom_rest import KiwoomREST
from config import settings


def raw_call(rest, stock_code, base_dt_mode):
    """
    base_dt_mode:
      'today'  → 오늘 날짜 명시
      'empty'  → 빈 문자열
      'absent' → 키 자체를 넣지 않음
    raw 응답의 row 개수와 처음/끝 시간을 반환.
    """
    body = {
        "stk_cd": stock_code,
        "tic_scope": "1",
        "upd_stkpc_tp": "1",
    }
    if base_dt_mode == "today":
        body["base_dt"] = datetime.now().strftime("%Y%m%d")
    elif base_dt_mode == "empty":
        body["base_dt"] = ""
    # 'absent'는 base_dt 키 자체를 안 넣음

    result = rest._request("/api/dostk/chart", "ka10080", body)
    rc = result.get("return_code")
    rows = result.get("stk_min_pole_chart_qry", []) or []
    n = len(rows)
    first = rows[0].get("cntr_tm") if n else "-"
    last = rows[-1].get("cntr_tm") if n else "-"
    return rc, n, first, last, result.get("return_msg", "")


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else "005930"

    token = get_access_token()
    if not token:
        print("❌ 토큰 발급 실패")
        return
    print(f"✅ 토큰 발급 완료\n")
    print(f"대상 종목: {code}   현재시각: {datetime.now().strftime('%H:%M:%S')}\n")

    rest = KiwoomREST(token, is_mock=settings.IS_MOCK)

    print("=" * 70)
    print(f"{'base_dt 방식':<14}{'return':>7}{'row수':>7}   처음 ~ 끝")
    print("=" * 70)

    for mode, label in [("today", "오늘명시"), ("empty", "빈문자열"), ("absent", "키없음")]:
        rc, n, first, last, msg = raw_call(rest, code, mode)
        print(f"{label:<14}{rc:>7}{n:>7}   {first} ~ {last}")
        if rc != 0:
            print(f"               └ msg: {msg}")

    print("=" * 70)
    print("\n해석:")
    print(" - 장중에 돌릴 것: '오늘명시'가 1개, 다른 게 많이 오면 그게 원인 확정.")
    print(" - 가장 많이 오는 방식으로 get_minute_candles의 base_dt 처리를 바꾸면 됨.")
    print("\n✅ 완료")


if __name__ == "__main__":
    main()