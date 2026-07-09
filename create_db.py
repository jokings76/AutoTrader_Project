"""
autotrader_db 데이터베이스 생성 스크립트.

PostgreSQL 컨테이너에 본인 봇 전용 DB를 만든다.
이미 존재하면 스킵 (재실행 안전).

실행: python create_db.py
"""
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT


# 시스템 DB 'postgres'로 접속해서 새 DB 생성
ADMIN_CONFIG = {
    "host":     "localhost",
    "port":     5432,
    "database": "postgres",     # 시스템 DB
    "user":     "admin",
    "password": "password123",
}

NEW_DB_NAME = "autotrader_db"


def create_database():
    print("=" * 60)
    print(f"새 DB 생성: {NEW_DB_NAME}")
    print("=" * 60)
    
    try:
        conn = psycopg2.connect(**ADMIN_CONFIG)
        # CREATE DATABASE는 트랜잭션 안에서 실행 못 함
        # → AUTOCOMMIT 모드 필요
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()
        
        # 1. 이미 존재하는지 확인
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (NEW_DB_NAME,)
        )
        exists = cur.fetchone()
        
        if exists:
            print(f"⚠️ '{NEW_DB_NAME}' 이미 존재. 스킵.")
        else:
            # 2. 새 DB 생성
            cur.execute(f'CREATE DATABASE "{NEW_DB_NAME}"')
            print(f"✅ '{NEW_DB_NAME}' 생성 완료!")
        
        # 3. 현재 DB 목록 확인
        cur.execute("""
            SELECT datname 
            FROM pg_database 
            WHERE datistemplate = false 
            ORDER BY datname
        """)
        dbs = [row[0] for row in cur.fetchall()]
        print(f"\n현재 DB 목록 ({len(dbs)}개): {dbs}")
        
        cur.close()
        conn.close()
        
        # 4. 새 DB로 접속 테스트
        print(f"\n--- '{NEW_DB_NAME}' 접속 테스트 ---")
        test_conn = psycopg2.connect(
            host="localhost",
            port=5432,
            database=NEW_DB_NAME,
            user="admin",
            password="password123",
        )
        test_cur = test_conn.cursor()
        test_cur.execute("SELECT current_database(), current_user")
        db, user = test_cur.fetchone()
        print(f"✅ 접속 성공: DB={db}, User={user}")
        
        # 테이블 목록 (비어있을 거)
        test_cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables 
            WHERE table_schema = 'public'
        """)
        count = test_cur.fetchone()[0]
        print(f"   현재 테이블 수: {count}개 (정상 - 비어있음)")
        
        test_cur.close()
        test_conn.close()
        
        print("\n✅ 모든 작업 완료")
        
    except Exception as e:
        print(f"❌ 에러 발생: {type(e).__name__}: {e}")


if __name__ == "__main__":
    create_database()