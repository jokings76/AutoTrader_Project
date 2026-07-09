import psycopg2

# 우리가 쓰던 DB 접속 정보 그대로 사용
conn = psycopg2.connect(
    host="localhost", port=5432, database="autotrader_db", user="admin", password="password123"
)
cur = conn.cursor()

print("=" * 40)
print("🎯 trades 테이블 컬럼 목록 확인")
print("=" * 40)

# trades 테이블의 컬럼 이름과 데이터 타입 가져오기
cur.execute("""
    SELECT column_name, data_type 
    FROM information_schema.columns 
    WHERE table_name = 'trades'
    ORDER BY ordinal_position;
""")

rows = cur.fetchall()
for row in rows:
    print(f"{row[0]:<15} | {row[1]}")

cur.close()
conn.close()