import os
import logging
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# 테스트 실행은 **실거래 로그와 다른 파일**에 쓴다 (2026-08-03).
# 08-03에 장중 테스트를 돌렸더니 autotrader.log에 가짜 매매가 56줄 섞여
# 들어갔다(`BUY [S03]`, `SELL [000006] 손절 -4.00%` 같은 것들). 봇 동작에는
# 영향이 없지만 **그날을 로그로 분석할 때 실거래와 구분이 안 된다** —
# 실제로 "마지막 매수는?"을 묻자 테스트 종목이 튀어나왔다.
# 테스트 스위트가 프로세스 시작 전에 이 환경변수를 세팅한다.
_TEST_MODE = os.environ.get("AUTOTRADER_TEST_LOG") == "1"
LOG_FILE = os.path.join(LOG_DIR, "test_run.log" if _TEST_MODE else "autotrader.log")

logger = logging.getLogger("AutoTrader")
logger.setLevel(logging.INFO)

if not logger.handlers:
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
