"""
autotrader_db 테이블 생성 스크립트.
테스트 코드(test_repository.py)의 요구사항을 모두 반영한 최종 버전.
"""
import psycopg2

DB_CONFIG = {
    "host":     "localhost",
    "port":     5432,
    "database": "autotrader_db",
    "user":     "admin",
    "password": "password123",
}

# ───────────────────────────────────────────────────
# 테이블 정의 (테스트 코드 맞춤형)
# ───────────────────────────────────────────────────

CREATE_TRADES = """
DROP TABLE IF EXISTS trades;

CREATE TABLE trades (
    id              SERIAL PRIMARY KEY,
    trade_date      DATE DEFAULT CURRENT_DATE,
    
    stock_code      VARCHAR(10) NOT NULL,
    stock_name      VARCHAR(50),
    
    -- 매수 정보
    buy_price       INT,
    buy_quantity    INT,
    buy_amount      BIGINT,  -- 👈 새로 추가 (매수 금액)
    strategy_phase  INT,
    entry_reason    VARCHAR(100),
    buy_time        TIMESTAMP DEFAULT NOW(),
    
    -- 상태 관리
    status          VARCHAR(20) DEFAULT 'open',  -- 'open' 또는 'closed'
    
    -- 매도 정보
    sell_price      INT,
    sell_quantity   INT,
    sell_amount     BIGINT,  -- 👈 새로 추가 (매도 금액)
    exit_reason     VARCHAR(100),
    sell_time       TIMESTAMP,
    
    -- 비용 및 수익
    fee             INT DEFAULT 0,
    tax             INT DEFAULT 0,
    profit_rate     NUMERIC(6,2),
    profit_amount   INT,
    
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_trades_date ON trades(trade_date);
CREATE INDEX idx_trades_code ON trades(stock_code);
CREATE INDEX idx_trades_status ON trades(status);
"""

CREATE_WATCH_LIST_LOG = """
DROP TABLE IF EXISTS watch_list_log;

CREATE TABLE watch_list_log (
    id                   SERIAL PRIMARY KEY,
    trade_date           DATE DEFAULT CURRENT_DATE,
    
    stock_code           VARCHAR(10) NOT NULL,
    stock_name           VARCHAR(50),
    added_time           TIMESTAMP DEFAULT NOW(),
    
    -- 테스트 코드 요구 컬럼들
    phase                INT,
    open_price           INT,
    current_price        INT,
    surge_rate           NUMERIC(6,2),
    volume_ratio         NUMERIC(10,2),
    ma5                  NUMERIC(10,2),
    is_bought            BOOLEAN DEFAULT FALSE,
    
    -- 등록 시 상태
    initial_price        INT,
    today_open           INT,
    
    -- 매수 결과
    bought_in_phase1     BOOLEAN DEFAULT FALSE,
    bought_in_phase2     BOOLEAN DEFAULT FALSE,
    phase1_attempts      INT DEFAULT 0,
    phase2_attempts      INT DEFAULT 0,
    
    -- 사후 분석 (장 마감 시 기록)
    today_high           INT,
    today_close          INT,
    high_pct             NUMERIC(6,2),
    close_pct            NUMERIC(6,2),
    
    created_at           TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_watch_date ON watch_list_log(trade_date);
CREATE INDEX idx_watch_code ON watch_list_log(stock_code);
"""

CREATE_DAILY_SUMMARY = """
DROP TABLE IF EXISTS daily_summary;

CREATE TABLE daily_summary (
    id                  SERIAL PRIMARY KEY,     -- 👈 새로 추가된 고유 번호(id)
    trade_date          DATE UNIQUE NOT NULL,   -- 기존 PRIMARY KEY에서 UNIQUE로 변경
    
    -- 테스트 코드 요구 컬럼들
    total_trades        INT DEFAULT 0,
    winning_trades      INT DEFAULT 0,
    losing_trades       INT DEFAULT 0,
    win_rate            NUMERIC(5,2),
    net_profit          INT DEFAULT 0,
    
    -- 기존 컬럼 유지
    total_buys          INT DEFAULT 0,
    total_sells         INT DEFAULT 0,
    time_stopped        INT DEFAULT 0,
    force_closed        INT DEFAULT 0,
    phase1_buys         INT DEFAULT 0,
    phase2_buys         INT DEFAULT 0,
    total_pnl           INT DEFAULT 0,
    total_buy_amount    BIGINT DEFAULT 0,
    total_sell_amount   BIGINT DEFAULT 0,
    phase1_pnl          INT DEFAULT 0,
    phase2_pnl          INT DEFAULT 0,
    watch_list_size     INT DEFAULT 0,
    watch_list_bought   INT DEFAULT 0,
    notes               TEXT,
    
    created_at          TIMESTAMP DEFAULT NOW()
);
"""

CREATE_SYSTEM_EVENTS = """
DROP TABLE IF EXISTS system_events;

CREATE TABLE system_events (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMP NOT NULL DEFAULT NOW(),
    event_type      VARCHAR(30),
    severity        VARCHAR(10),
    event_message   TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_events_timestamp ON system_events(timestamp);
CREATE INDEX idx_events_type      ON system_events(event_type);
"""

# ───────────────────────────────────────────────────
# 실행 로직
# ───────────────────────────────────────────────────

TABLES = [
    ("trades",          CREATE_TRADES,           "매매 기록"),
    ("watch_list_log",  CREATE_WATCH_LIST_LOG,   "Watch List 기록"),
    ("daily_summary",   CREATE_DAILY_SUMMARY,    "일일 요약"),
    ("system_events",   CREATE_SYSTEM_EVENTS,    "시스템 이벤트"),
]

def create_tables():
    print("=" * 60)
    print("autotrader_db 테이블 생성 (초기화 및 재설정)")
    print("=" * 60)
    
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        
        for table_name, sql, description in TABLES:
            print(f"\n[{table_name}] {description}")
            try:
                cur.execute(sql)
                conn.commit()
                print(f"  ✅ 생성/확인 완료")
            except Exception as e:
                conn.rollback()
                print(f"  ❌ 실패: {e}")
                raise
        
        cur.close()
        conn.close()
        print("\n✅ 모든 테이블 생성 완료")
        
    except Exception as e:
        print(f"\n❌ 에러: {type(e).__name__}: {e}")
        raise

if __name__ == "__main__":
    create_tables()