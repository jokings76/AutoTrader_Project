"""
네이버 금융 테마 목록 + 테마별 종목코드 수집.
GitHub Actions에서 주기 실행되어 theme_data.json을 생성/커밋.

출력 구조 (core/theme_manager.py의 fetch_themes_from_github()가
"codes"/"stocks"/"items"/"data" 키 중 하나를 리스트로 읽으므로 "codes" 사용):
{
  "themes": [
    {"name": "2차전지", "codes": ["005930", "000660", ...]},
    ...
  ],
  "updated_at": "2026-07-25T09:00:00"
}
"""

import json
import re
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

BASE = "https://finance.naver.com"
LIST_URL = f"{BASE}/sise/theme.naver"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
REQUEST_DELAY_SEC = 0.3  # 테마 상세 페이지 요청 간 딜레이 (과도한 요청 방지)
MAX_PAGES = 4            # 테마 목록 페이지네이션 (사이트 실제 페이지 수에 맞춰 조정)


def fetch_theme_list() -> list[dict]:
    """테마 목록 페이지(여러 페이지 순회)에서 {"name":..., "no":...} 리스트 수집."""
    themes = []
    seen_no = set()
    for page in range(1, MAX_PAGES + 1):
        resp = requests.get(
            LIST_URL, params={"page": page}, headers=HEADERS, timeout=10
        )
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")

        rows_found = 0
        for a in soup.select("table.type_1 a[href*='sise_group_detail.naver']"):
            href = a.get("href", "")
            m = re.search(r"no=(\d+)", href)
            if not m:
                continue
            no = m.group(1)
            name = a.get_text(strip=True)
            if not name or no in seen_no:
                continue
            seen_no.add(no)
            themes.append({"name": name, "no": no})
            rows_found += 1

        if rows_found == 0:
            break  # 마지막 페이지 지남
        time.sleep(REQUEST_DELAY_SEC)

    return themes


def fetch_theme_stock_codes(theme_no: str) -> list[str]:
    """테마 상세 페이지에서 소속 종목코드 전체 수집."""
    url = f"{BASE}/sise/sise_group_detail.naver"
    resp = requests.get(
        url, params={"type": "theme", "no": theme_no}, headers=HEADERS, timeout=10
    )
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "html.parser")

    codes = []
    for a in soup.select("a[href*='/item/main.naver?code=']"):
        m = re.search(r"code=(\d{6})", a.get("href", ""))
        if m:
            code = m.group(1)
            if code not in codes:
                codes.append(code)
    return codes


def main():
    print("테마 목록 수집 중...")
    theme_list = fetch_theme_list()
    print(f"테마 {len(theme_list)}개 발견")

    result_themes = []
    for i, theme in enumerate(theme_list, 1):
        try:
            codes = fetch_theme_stock_codes(theme["no"])
            if codes:
                result_themes.append({"name": theme["name"], "codes": codes})
                print(f"[{i}/{len(theme_list)}] {theme['name']}: {len(codes)}개 종목")
            else:
                print(f"[{i}/{len(theme_list)}] {theme['name']}: 종목 0개 (스킵)")
        except Exception as e:
            print(f"[{i}/{len(theme_list)}] {theme['name']} 실패: {e}")
        time.sleep(REQUEST_DELAY_SEC)

    output = {
        "themes": result_themes,
        "updated_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
    }
    with open("theme_data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"완료: 테마 {len(result_themes)}개 → theme_data.json")


if __name__ == "__main__":
    main()
