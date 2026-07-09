"""
PostgreSQL 접속 테스트.
실행: python test_db_connection.py
"""
import psycopg2


# DB 접속 정보 (docker-compose.yml에서 추출)
DB_CONFIG = {
    "host":     "localhost",
    "port":     5432,
    "database": "analytics_db",
    "user":     "admin",
    "password": "password123",
}


def test_connection():
    """기본 접속 테스트"""
    print("=" * 60)
    print("PostgreSQL 접속 테스트")
    print("=" * 60)
    print(f"  Host:     {DB_CONFIG['host']}")
    print(f"  Port:     {DB_CONFIG['port']}")
    print(f"  Database: {DB_CONFIG['database']}")
    print(f"  User:     {DB_CONFIG['user']}")
    print()
    
    try:
        # 접속 시도
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        
        # 1. PostgreSQL 버전 조회
        cur.execute("SELECT version();")
        version = cur.fetchone()[0]
        print(f"✅ 접속 성공!")
        print(f"   PostgreSQL 버전: {version}\n")
        
        # 2. 현재 데이터베이스 목록
        cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false;")
        databases = [row[0] for row in cur.fetchall()]
        print(f"   현재 DB 목록: {databases}\n")
        
        # 3. 현재 테이블 목록 (analytics_db 안)
        cur.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public'
            ORDER BY table_name;
        """)
        tables = [row[0] for row in cur.fetchall()]
        print(f"   현재 테이블 ({len(tables)}개): {tables if tables else '(없음, 정상)'}\n")
        
        # 4. 정리
        cur.close()
        conn.close()
        print("✅ 모든 검증 통과")
        return True
        
    except psycopg2.OperationalError as e:
        print(f"❌ 접속 실패 (OperationalError):")
        print(f"   {e}")
        print("\n원인 가능성:")
        print("  1. PostgreSQL 컨테이너가 정지됨 (Docker Desktop 확인)")
        print("  2. 포트 5432 충돌 (다른 PostgreSQL 실행 중?)")
        print("  3. 방화벽 차단")
        return False
        
    except psycopg2.errors.InvalidPassword:
        print(f"❌ 비밀번호 틀림")
        return False
        
    except Exception as e:
        print(f"❌ 예상치 못한 에러: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    test_connection()