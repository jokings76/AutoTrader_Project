import requests
import json
from config import settings
from utils.logger import logger


def get_access_token():
    """키움 REST API 모의투자용 접근토큰 발급"""
    url = "https://mockapi.kiwoom.com/oauth2/token"

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
            logger.info("🔑 [성공] 모의투자용 REST API 토큰 발급 완료!")
            return token
        else:
            logger.error(f"❌ 토큰을 찾지 못했습니다: {res_json.get('return_msg')}")
            return None

    except Exception as e:
        logger.error(f"❌ 인증 서버 연결 오류: {e}")
        return None


def send_telegram(message, target='signal'):
    """
    텔레그램 알림 전송
    
    Args:
        message: 보낼 메시지 본문
        target: 'signal' (개인 채팅) 또는 'order' (주문 그룹 채팅)
    """
    chat_id = (settings.TELEGRAM_CHAT_ID_ORDER 
               if target == 'order' 
               else settings.TELEGRAM_CHAT_ID_SIGNAL)
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