# ══════════════════════════════════════════════════════════════════
# ⚠️ [삭제 예정 / DEPRECATED] — 2026-08-02 표시
#
# 타인 환경의 로컬 REST 서버용 초기 프로토타입. 라이브는 api/kiwoom_rest.py 하나뿐이다. core/theme_manager_v1_legacy.py가 이걸 import하므로 삭제 대신 표시만 한다.
#
# **어디서도 import되지 않는다**(main.py 기준 도달성 분석으로 확인).
# 지금 살아있는 전략은 1A / Pullback 두 개뿐이며 둘 다 틱 구동
# (체결강도 FID228 3초 연속 + 대량체결 버스트)으로 동작한다.
# 이 파일을 현재 동작의 근거로 삼지 말 것.
#
# 삭제해도 git 히스토리로 복구 가능하다 — 다윤님이 판단해서 정리하면 된다.
# ══════════════════════════════════════════════════════════════════

import requests
import logging

logger = logging.getLogger(__name__)

# JO님의 키움 REST API 로컬 서버 주소 (환경에 맞게 수정 필요)
BASE_URL = "http://127.0.0.1:5000"


def get_program_net_buy(stock_code: str) -> int:
    """
    키움 REST API를 통해 특정 종목의 당일 프로그램 순매수 수량을 가져옵니다.

    :param stock_code: 종목코드 (6자리 문자열, 예: '005930')
    :return: 당일 프로그램 순매수 수량 (단위: 주, 매도 우위일 경우 마이너스)
    """
    # 1. 프로그램 매매동향 요청을 위한 엔드포인트 및 파라미터 세팅
    endpoint = "/api/v1/tr/opt10013"  # 키움 프로그램매매합계 TR 예시
    url = f"{BASE_URL}{endpoint}"

    params = {
        "종목코드": stock_code,
        "금액수량구분": "1",  # 1: 수량 기준, 2: 금액 기준
        "시장구분": "0",  # 0: 전체, 1: 코스피, 2: 코스닥 (보통 0으로 조회)
    }

    try:
        # 2. API 요청 보내기
        response = requests.get(url, params=params, timeout=3)

        if response.status_code == 200:
            result = response.json()

            # 3. 키움 API 반환 데이터에서 프로그램 순매수 수량 추출
            # 키움 TR 출력 데이터 구조에 따라 '종가'나 '당일합계' 등 key 이름 확인 필요
            output_data = result.get("output", {})  # 💡 누락되었던 변수 정의 추가 완료!

            # 당일 프로그램 순매수 수량 (문자열로 들어오므로 부호 처리 후 정수 변환)
            raw_val = output_data.get("순매수수량", "0")
            program_net_buy = int(raw_val.replace(",", ""))  # 콤마 제거 후 int 변환

            return program_net_buy

        else:
            logger.error(f" API 오류 ({response.status_code}): {response.text}")
            return 0

    except Exception as e:
        logger.error(f" 프로그램 수급 조회 중 예외 발생 (종목: {stock_code}): {e}")
        return 0
