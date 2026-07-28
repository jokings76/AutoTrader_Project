import json
import os
from datetime import datetime
from db.connection import get_cursor
from psycopg2.extras import execute_values


def migrate_past_data(log_folder_path):
    sql = """
        INSERT INTO daily_themes (trade_date, stock_code, theme_name)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    """

    # 1. 폴더 내 모든 파일 탐색
    for filename in os.listdir(log_folder_path):
        if filename.endswith(".jsonl"):
            # 파일명에서 날짜 추출 (예: 2026-03-01.json)
            date_str = filename.replace(".jsonl", "")
    for filename in os.listdir(log_folder_path):
        print(f"DEBUG: 파일 확인 중 -> {filename}")  # 이 줄 추가
        if filename.endswith(".jsonl"):
            # ... 나머지 코드

            with open(
                os.path.join(log_folder_path, filename), "r", encoding="utf-8"
            ) as f:
                data = json.load(f)

                # 2. 데이터 구조 파싱 (JO님의 로그 형식에 맞게 수정 필요)
                # 예: {'themes': [{'name': 'AI', 'codes': ['005930', ...]}, ...]}
                records = []
                for theme in data.get("themes", []):
                    theme_name = theme.get("name")
                    for code in theme.get("codes", []):
                        records.append((date_str, code, theme_name))

                # 3. DB 적재
                with get_cursor() as cur:
                    execute_values(cur, sql, records)
                print(f"✅ {date_str} 데이터 {len(records)}개 적재 완료")

                # 실행: 로그 파일들이 있는 폴더 경로를 입력하세요
                migrate_past_data("./observations")
