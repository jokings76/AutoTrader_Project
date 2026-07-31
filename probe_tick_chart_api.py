"""ka10079(주식틱차트조회요청) 응답 스키마 탐색기 (읽기 전용, 2026-07-31).

왜 필요한가
──────────
1A(evaluate_1a_leading_strength)는 체결강도(compute_strength)를 틱 단위로
계산하는데, 지금 백테스트(daily_backtest.py)는 1분봉만 갖고 있어 이 부분을
재현할 수 없다. 대신증권(CYBOS Plus) 틱데이터 확보를 논의했었지만, 키움
REST API 자체에도 ka10079(주식틱차트조회요청)라는 틱 단위 엔드포인트가
있다는 게 확인돼(공식 API 목록, ka10080=분봉/ka10081=일봉과 같은 '차트'
카테고리) — 이미 쓰고 있는 계정으로 바로 시도해볼 수 있다.

확인하려는 것
────────────
1. 이 엔드포인트가 실제로 응답하는지 (path/body 파라미터 추정이 맞는지)
2. 응답에 매수/매도 구분(체결강도 계산에 필수) 필드가 있는지, 아니면
   ka10080처럼 OHLCV만 주는 '집계봉'인지 — 후자면 TradeFlowTracker.add_tick()
   가 요구하는 형태(가격/거래량/매수·매도 side)로는 못 쓴다.

주의
  - 조회(inquiry) 호출만 한다.
  - 장중엔 돌리지 말 것(429 예산 문제, 이미 포화 상태) — 장 시작 전/마감 후 실행.
"""
import io
import json
import sys
import time

from api.auth import get_access_token
from config import settings

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

INTERVAL_SEC = 1.5

# body 파라미터는 ka10080(분봉)과 같은 계열로 추정 — tic_scope가 분봉에선
# "분" 단위였는데 틱차트에선 "틱 수"(예: 1=매틱마다 1봉)를 의미할 가능성이 큼.
CANDIDATES = [
    {"stk_cd": "005930", "tic_scope": "1", "upd_stkpc_tp": "1"},
    {"stk_cd": "005930", "tic_scope": "1"},
]


def probe(rest, body: dict):
    print(f"\n{'='*70}\n[ka10079] /api/dostk/chart  body={body}")
    try:
        res = rest._request("/api/dostk/chart", "ka10079", body)
    except Exception as e:
        print(f"  예외: {e}")
        return
    code = res.get("return_code")
    msg = res.get("return_msg", "")
    print(f"  return_code={code} return_msg={msg!r}")
    if code != 0:
        return
    print(f"  성공! 최상위 키: {list(res.keys())}")
    for k, v in res.items():
        if isinstance(v, list) and v:
            print(f"  리스트 '{k}' {len(v)}건 — 첫 3행 필드:")
            for row in v[:3]:
                print("   ", json.dumps(row, ensure_ascii=False)[:500])
            break


def main():
    token = get_access_token()
    if not token:
        print("토큰 발급 실패 — config.ini 확인")
        return 1
    from api.kiwoom_rest import KiwoomREST
    rest = KiwoomREST(token, is_mock=getattr(settings, "IS_MOCK", True))
    print(f"host={rest.host}")

    for body in CANDIDATES:
        probe(rest, body)
        time.sleep(INTERVAL_SEC)

    print(f"\n{'='*70}")
    print("응답 필드에 매수/매도 구분(예: 'sel_bid_tp' 같은 체결구분 코드)이")
    print("있으면 TradeFlowTracker.add_tick() 형식으로 변환하는 로더를 바로")
    print("만들 수 있음 — 없으면(OHLCV만) 근사 프록시로만 쓸 수 있음.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
