import requests
from bs4 import BeautifulSoup
import json
import time

def fetch_all_naver_themes():
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    all_themes = []
    page = 1
    last_page_themes = [] # 이전 페이지 테마 기록용
    
    while True:
        # 안전장치 1: 혹시라도 20페이지를 넘어가면 강제 종료 (네이버 테마는 보통 7~8페이지에서 끝남)
        if page > 20:
            print("페이지 한계 도달 (강제 종료)")
            break
            
        url = f"https://finance.naver.com/sise/theme.naver?&page={page}"
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        themes = soup.select('td.col_type1 > a')
        
        if not themes: 
            break
            
        # 현재 페이지의 테마 이름만 리스트로 추출
        current_page_themes = []
        for theme in themes:
            theme_name = theme.text.strip()
            if theme_name:
                current_page_themes.append(theme_name)
                
        # 안전장치 2 (핵심): 이번 페이지 테마가 이전 페이지 테마와 완전히 똑같다면? (마지막 페이지를 넘어갔다는 뜻)
        if current_page_themes == last_page_themes:
            print(f"더 이상 새로운 테마가 없습니다. 수집 종료.")
            break
            
        all_themes.extend(current_page_themes)
        last_page_themes = current_page_themes # 다음 비교를 위해 현재 페이지 기록 저장
            
        print(f"{page}페이지 테마 수집 완료...")
        page += 1
        time.sleep(1.5) # 서버 과부하 방지
        
    return all_themes

if __name__ == "__main__":
    print("🚀 네이버 테마 자동 수집 시작...")
    themes_list = fetch_all_naver_themes()
    
    data = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_count": len(themes_list),
        "themes": themes_list
    }
    
    with open('theme_data.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        
    print(f"✅ 총 {len(themes_list)}개의 테마가 'theme_data.json'에 성공적으로 저장되었습니다!")
