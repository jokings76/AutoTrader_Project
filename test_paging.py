from api.auth import get_access_token
from api.kiwoom_rest import KiwoomREST
from config import settings

print('1. 토큰 발급 중...')
token = get_access_token()
if not token:
    print('토큰 발급 실패!')
    exit()

print('2. REST 클라이언트 생성...')
rest = KiwoomREST(token, is_mock=settings.IS_MOCK)

print('3. 삼성전자(005930) 1분봉 90개 요청 (페이징 강제 발생 시도)...')
candles = rest.get_minute_candles('005930', interval=1, count=90)

print(f'\n=== [테스트 결과] ===')
print(f'최종 수집된 봉 개수: {len(candles)}개')

if len(candles) >= 60:
    print('✅ 성공! 60개 이상 정상 수집됨 (페이징 및 캐시 정상 작동)')
    print(f'   - 가장 최근 봉 시간: {candles[0]["time_str"]}')
    print(f'   - 가장 오래된 봉 시간: {candles[-1]["time_str"]}')
elif len(candles) > 0:
    print('⚠️ 주의: 60개 미만 수집됨. (모의투자 서버 데이터 부족 가능성)')
else:
    print('❌ 실패: 데이터가 0개입니다.')