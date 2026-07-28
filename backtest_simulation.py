import os
from collections import defaultdict

def analyze_trade_lifecycle(target_date):
    print(f"=== [{target_date} 종목별 매매 생애주기 분석 리포트] ===")
    
    log_dir = "logs"
    if not os.path.exists(log_dir):
        print("⚠️ 'logs' 폴더를 찾을 수 없습니다.")
        return

    date_str_compact = target_date.replace("-", "")
    target_files = [f for f in os.listdir(log_dir) if date_str_compact in f]
    
    if not target_files:
        print(f"⚠️ {target_date}에 해당하는 로그 파일을 찾을 수 없습니다.")
        return

    # 종목별로 로그를 묶기 위한 딕셔너리
    stock_logs = defaultdict(list)

    for filename in target_files:
        filepath = os.path.join(log_dir, filename)
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                # 로그 내에서 종목코드(6자리 숫자)나 주요 키워드가 포함된 경우 수집
                if any(k in line for k in ["매수", "매도", "조건", "포착", "Phase", "실패", "차단", "수량"]):
                    stock_logs["전체_흐름"].append(line.strip())

    print(f"\n[오늘 감지된 주요 매매 흐름 전문 (총 {len(stock_logs['전체_흐름']}건)]")
    print("-" * 90)
    
    # 전체 내용을 누락 없이 순서대로 깔끔하게 출력
    for idx, log in enumerate(stock_logs["전체_흐름"], 1):
        print(f"{idx:3d} | {log}")
        
    print("-" * 90)

if __name__ == "__main__":
    target_date = "2026-07-22"
    analyze_trade_lifecycle(target_date)