"""프로그램매매 REST 엔드포인트 탐색기 (읽기 전용, 2026-07-31).

왜 필요한가
──────────
core/program_flow.py에 유입 추적 인프라는 만들어 뒀지만, 키움 REST에서
'프로그램 순매수'를 어떤 api_id/path로 주는지는 계정 문서에서 확인이 필요하다.
이 스크립트는 후보 엔드포인트를 하나씩 조회만 해보고(주문 없음) 어떤 게
실제로 응답하는지, 응답에 어떤 필드가 들어있는지 찍어준다.

사용법
    python probe_program_api.py                 # 기본 후보군 전부 시도
    python probe_program_api.py ka90003 /api/dostk/pgtrde   # 특정 조합만 시도

주의
  - 조회(inquiry) 호출만 한다. 주문 API는 후보에 넣지 않았다.
  - 호출 사이에 1.2초 간격을 둔다 — 이 계정은 이미 429가 잦다(07-30 기준
    하루 2,469건). 장중에 돌리지 말고 **장 시작 전이나 장 마감 후**에 실행할 것.
  - 후보 목록은 추정치다. 응답이 전부 실패하면 키움 REST 문서(또는 영웅문
    OpenAPI 가이드)의 '프로그램매매' 섹션에서 실제 api_id를 찾아
    CANDIDATES에 추가한 뒤 다시 실행하면 된다.
"""
import json
import sys
import time

from api.auth import get_access_token
from config import settings

# (api_id, path, body) 후보. 키움 REST는 api-id 헤더 + path 조합으로 TR을 지정한다.
# 실제 문서에서 확인되는 대로 이 목록만 고치면 된다.
CANDIDATES = [
    # 종목별 프로그램매매 추이 계열
    ("ka90003", "/api/dostk/pgtrde", {"stk_cd": "005930"}),
    ("ka90004", "/api/dostk/pgtrde", {"stk_cd": "005930"}),
    ("ka10046", "/api/dostk/stkinfo", {"stk_cd": "005930"}),
    # 시장 전체 프로그램 순매수 상위 계열 (종목 수와 무관하게 1콜 -> 호출예산상 최선)
    ("ka90005", "/api/dostk/rkinfo", {"mrkt_tp": "P00101", "amt_qty_tp": "1"}),
    ("ka90006", "/api/dostk/rkinfo", {"mrkt_tp": "P00101", "amt_qty_tp": "1"}),
    ("ka10047", "/api/dostk/rkinfo", {"mrkt_tp": "001"}),
]

INTERVAL_SEC = 1.2  # 429 방지 (계정이 이미 포화 상태)


def probe(rest, api_id: str, path: str, body: dict):
    print(f"\n{'='*70}\n[{api_id}] {path}  body={body}")
    try:
        res = rest._request(path, api_id, body)
    except Exception as e:
        print(f"  예외: {e}")
        return
    code = res.get("return_code")
    msg = res.get("return_msg", "")
    if code != 0:
        print(f"  실패 return_code={code} msg={msg}")
        return
    print(f"  성공! 최상위 키: {list(res.keys())}")
    for k, v in res.items():
        if isinstance(v, list) and v:
            print(f"  리스트 '{k}' {len(v)}건 — 첫 행 필드:")
            print("   ", json.dumps(v[0], ensure_ascii=False)[:600])
            break


def main():
    token = get_access_token()
    if not token:
        print("토큰 발급 실패 — config.ini 확인")
        return 1
    from api.kiwoom_rest import KiwoomREST
    rest = KiwoomREST(token, is_mock=getattr(settings, "IS_MOCK", True))
    print(f"host={rest.host}")

    if len(sys.argv) >= 3:
        cands = [(sys.argv[1], sys.argv[2], {"stk_cd": "005930"})]
    else:
        cands = CANDIDATES

    for api_id, path, body in cands:
        probe(rest, api_id, path, body)
        time.sleep(INTERVAL_SEC)

    print(f"\n{'='*70}")
    print("성공한 조합이 있으면 그 api_id/path와 응답 필드명을 알려주세요 —")
    print("core/program_flow.py에 소스로 연결하겠습니다.")
    print("전부 실패하면 키움 REST 문서의 '프로그램매매' api_id를 확인해")
    print("이 파일의 CANDIDATES에 추가한 뒤 다시 실행하면 됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
