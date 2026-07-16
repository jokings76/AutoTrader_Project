import requests
from bs4 import BeautifulSoup
import json
import time

def fetch_all_naver_themes():
    # 네이버 서버의 봇 차단 방어용 헤더
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    all_themes = []
    page = 1
    
    while True:
        url = f"https://finance.naver.com/sise/theme.naver?&page={page}"
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 🔥 수정된 부분: 네이버 실제 HTML 구조에 맞게 돋보기(선택자) 변경
        themes = soup.select('td.col_type1 > a')
        
        # 마지막 페이지 도달 시 종료
        if not themes: 
            break
            
        for theme in themes:
            try:
                theme_name = theme.text.strip()
                if theme_name: # 빈 줄 무시
                    all_themes.append(theme_name)
            except Exception:
                continue
                
        print(f"{page}페이지 테마 수집 완료...")
        page += 1
        time.sleep(1.5) # 서버 과부하 방지 (1.5초 대기)
        
    return all_themes

if __name__ == "__main__":
    print("🚀 네이버 테마 자동 수집 시작...")
    themes_list = fetch_all_naver_themes()
    
    # 결과를 저장할 딕셔너리 구조 생성 (업데이트 시간 포함)
    data = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_count": len(themes_list),
        "themes": themes_list
    }
    
    # JSON 파일로 내보내기 (한글 깨짐 방지: ensure_ascii=False)
    with open('theme_data.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        
    print(f"✅ 총 {len(themes_list)}개의 테마가 'theme_data.json'에 성공적으로 저장되었습니다!")
