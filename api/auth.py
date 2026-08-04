import requests
import json
from config import settings
from utils.logger import logger


def get_access_token():
    """키움 REST API 접근토큰 발급 (IS_MOCK에 따라 모의/실전 서버 자동 선택)

    ⚠️ 2026-08-04 수정: 예전엔 이 URL이 mockapi로 **하드코딩**돼 있었다.
    IS_MOCK=false로 바꿔도 토큰만 모의 서버에서 받아오므로, 실전 앱키로는
    발급 자체가 실패해 `RuntimeError("토큰 발급 실패")`로 기동이 통째로
    죽는다(main.setup). 설령 발급됐더라도 KiwoomREST/KiwoomWS는 REAL_HOST를
    보므로 모든 요청이 인증 거부된다.
    호스트 문자열은 KiwoomREST.MOCK_HOST/REAL_HOST와 같은 값이지만, 이
    모듈이 api.kiwoom_rest를 import하면 순환 import가 생기므로 여기 둔다.
    """
    url = (
        "https://mockapi.kiwoom.com/oauth2/token"
        if settings.IS_MOCK
        else "https://api.kiwoom.com/oauth2/token"
    )
    mode = "모의투자" if settings.IS_MOCK else "실전투자"

    payload = {
        "grant_type": "client_credentials",
        "appkey": settings.APP_KEY,
        "secretkey": settings.SECRET_KEY,
    }
    headers = {"Content-Type": "application/json;charset=UTF-8"}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        res_json = response.json()

        token = res_json.get("token")

        if token:
            logger.info(f"🔑 [성공] {mode} REST API 토큰 발급 완료!")
            return token
        else:
            logger.error(
                f"❌ {mode} 토큰을 찾지 못했습니다 (url={url}): "
                f"{res_json.get('return_msg')}"
            )
            return None

    except Exception as e:
        logger.error(f"❌ 인증 서버 연결 오류 ({mode}, url={url}): {e}")
        return None


def send_telegram(message, target='signal'):
    """
    텔레그램 알림 전송
    
    Args:
        message: 보낼 메시지 본문
        target: 'signal' (개인 채팅) 또는 'order' (주문 그룹 채팅)
    """
    if target == "order":
        chat_id = settings.TELEGRAM_CHAT_ID_ORDER
    elif target == "closing_bet":
        chat_id = settings.TELEGRAM_CHAT_ID_CLOSING_BET
    else:
        chat_id = settings.TELEGRAM_CHAT_ID_SIGNAL
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
        result = response.json()

        if not result.get("ok"):
            logger.warning(
                f"⚠️ 텔레그램 전송 실패 (target={target}): "
                f"{result.get('description', '알 수 없는 오류')}"
            )
            return False
        return True

    except requests.Timeout:
        logger.warning(f"⚠️ 텔레그램 전송 타임아웃 (target={target})")
        return False
    except Exception as e:
        logger.warning(f"⚠️ 텔레그램 전송 예외 (target={target}): {e}")
        return False
