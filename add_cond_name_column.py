"""1회성 마이그레이션: watch_list_log에 cond_name 컬럼 추가 (2026-07-31)

목적: 어떤 조건검색식(주도주상위/눌림목자동/돌파자동매매용)이 이 종목을
편입시켰는지를 매수 여부와 무관하게 보존 — 기존엔 trades.entry_reason에만
"[조건명] ..." 프리픽스로 남아서 매수 안 된 후보는 출처가 영구 소실됐고,
그 결과 일일 백테스트(core/daily_backtest.py)가 조건검색식별 진입 로직
차이(예: 눌림목자동 skip_setup_check)를 전혀 재현할 수 없었다.

비파괴적 ALTER TABLE ADD COLUMN IF NOT EXISTS만 실행 — 기존 행/컬럼 영향 없음.
"""
from db.connection import get_cursor

if __name__ == "__main__":
    with get_cursor() as cur:
        cur.execute(
            "ALTER TABLE watch_list_log ADD COLUMN IF NOT EXISTS cond_name VARCHAR(50)"
        )
    print("OK: watch_list_log.cond_name 컬럼 추가 완료 (이미 있었으면 무변경)")
