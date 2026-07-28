"""배치파일(start_trader.bat)에서 호출. cmd.exe의 한글 인코딩 문제를 피하려고
텔레그램 메시지는 .bat에 직접 넣지 않고 이 .py 파일에 둔다."""
from api.auth import send_telegram

send_telegram(
    "⏰ 작업 스케줄러 실행 감지 - "
    "자동매매 봇 기동을 시작합니다.",
    target="signal",
)
