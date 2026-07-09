"""
로깅 설정 — root logger에 핸들러를 부착하여
모든 모듈(core.*, api.*, db.*, __main__)의 로그를 파일+콘솔에 기록한다.

기존엔 'AutoTrader' 이름 로거에만 핸들러를 붙여서
StrategyManager(core.strategy_manager) 등의 로그가 파일에 안 남는 문제가 있었음.
"""
import os
import logging
from datetime import datetime

# 프로젝트 루트 기준 logs 디렉토리 (utils/logger.py → 두 단계 위가 루트)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# 매일 날짜별로 파일 이름 생성 (예: system_20260521.log)
today_str = datetime.now().strftime('%Y%m%d')
LOG_FILE = os.path.join(LOG_DIR, f'system_{today_str}.log')

# 로그 기록 형태 (시간 - 메시지)
formatter = logging.Formatter('%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# 1. 파일에 기록
file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
file_handler.setFormatter(formatter)

# 2. 까만 창(터미널)에도 동시에 출력
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)

# ★ 핸들러를 root logger에 부착 → 모든 모듈 로그가 파일+콘솔로 전파됨
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
if not root_logger.handlers:        # 재import 시 핸들러 중복 방지
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)

# 외부 라이브러리 잡음 억제 (필요 없으면 이 블록 삭제 가능)
for _noisy in ("websockets", "asyncio", "urllib3", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# 기존 코드 호환: main.py 등의 `from utils.logger import logger`
# (핸들러는 root에 있고, 이 로거는 root로 전파되므로 정상 출력 + 중복 없음)
logger = logging.getLogger('AutoTrader')
logger.setLevel(logging.INFO)