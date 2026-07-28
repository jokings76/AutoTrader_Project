import time
import threading
import requests
from datetime import date
import psycopg2.extras

from utils.logger import logger
from db.connection import get_cursor


class ThemeManager:
    """깃허브에서 주도 테마를 가져와서 종목이 주도 테마인지 판별하는 클래스.

    주도테마 판별 (2026-07-06 재설계):
      - 기존 프로그램 순매수 기반 판별은 로컬서버(127.0.0.1:5000 미존재)로 제거.
      - 테마별 상위 N종목의 실시간 등락률 중 최고값이 임계값 이상이면
        해당 테마를 '주도테마'로 인정. rest_api(KiwoomREST).get_stock_change_rate() 사용.
    """

    LEADER_THRESHOLD = 13.0  # 대장주 등락률 임계값(%)
    POLL_INTERVAL_SEC = 30  # 주도테마 재계산 주기(초)

    def __init__(self, rest_api=None):
        """rest_api: KiwoomREST 인스턴스. get_change_rate_ranking() 호출용.
        None이면 등락률 조회 불가 → 주도테마 판별 항상 False."""
        self.rest_api = rest_api
        self.themes = []
        self.code_to_theme: dict[str, str] = {}
        self.leading_themes: set[str] = set()  # 현재 주도테마로 판정된 theme_name
        self.last_update = 0.0

    def fetch_themes_from_github(self):
        url = "https://raw.githubusercontent.com/jokings76/AutoTrader_Project/main/theme_data.json"
        try:
            response = requests.get(url, timeout=10)
            data = response.json()

            if "themes" in data:
                self.themes = data["themes"]
                self.code_to_theme = {}

                for theme in self.themes:
                    if not isinstance(theme, dict):
                        continue

                    code_list = None
                    for key in ["codes", "stocks", "items", "data"]:
                        if key in theme and isinstance(theme[key], list):
                            code_list = theme[key]
                            break

                    if not code_list:
                        continue

                    theme_name = theme.get("name", "알 수 없음")
                    for code in code_list:
                        clean_code = str(code).lstrip("A")
                        if clean_code.isdigit() and len(clean_code) == 6:
                            self.code_to_theme[clean_code] = theme_name

                self.last_update = time.time()
                self.save_themes_to_db()
                logger.info(
                    f"✅ 주도테마 {len(self.themes)}개 로드완료 (종목 수: {len(self.code_to_theme)}개)"
                )

                # 로드 직후 즉시 1회 주도테마 계산
                self.update_leading_themes()

            else:
                logger.warning(
                    "⚠️ theme_data.json 구조가 다릅니다. 'themes' 키가 있는지 확인해 주세요."
                )

        except Exception as e:
            logger.error(f"❌ 주도테마 가져오기 실패: {e}")

    def save_themes_to_db(self):
        """현재 로드된 테마 정보를 daily_themes 테이블에 저장"""
        today = date.today()
        sql_delete = "DELETE FROM daily_themes WHERE trade_date = %s"
        sql_insert = """
            INSERT INTO daily_themes (trade_date, stock_code, theme_name)
            VALUES (%s, %s, %s)
        """
        data_list = [(today, code, theme) for code, theme in self.code_to_theme.items()]
        if not data_list:
            return
        try:
            with get_cursor() as cur:
                cur.execute(sql_delete, (today,))
                psycopg2.extras.execute_values(cur, sql_insert, data_list)
            logger.info(f"💾 {len(data_list)}개 종목 테마 데이터 DB 저장 완료")
        except Exception as e:
            logger.error(f"❌ DB 저장 실패: {e}")

    def update_leading_themes(self):
        """시장 전체 등락률 상위 랭킹(ka10027, 코스피/코스닥 각 1회)을 조회해
        보유 중인 종목-테마 매핑에 역으로 대입 → 임계값 이상인 종목이 속한 테마를 주도테마로 인정.
        (테마별 개별 조회 대비 REST 호출 800여회 → 2회로 축소, 실거래 REST 스로틀과의 경합 제거)"""
        if not self.code_to_theme:
            return
        if not self.rest_api:
            logger.warning(
                "⚠️ [주도테마] rest_api 미설정 — 등락률 조회 불가, 판별 스킵"
            )
            return

        logger.info("🔄 [주도테마] 등락률 상위 랭킹 기반 재계산 시작...")
        start_time = time.time()
        new_leading = set()
        leader_stock: dict[str, tuple] = {}  # theme_name -> (code, rate), 로그용

        for mrkt_tp in ("001", "101"):
            try:
                ranking = self.rest_api.get_change_rate_ranking(mrkt_tp)
            except Exception as e:
                logger.error(f"❌ [{mrkt_tp}] 등락률 랭킹 조회 실패: {e}")
                continue

            for item in ranking:
                code = item.get("stk_cd", "")
                theme_name = self.code_to_theme.get(code)
                if not theme_name or theme_name in new_leading:
                    continue
                try:
                    rate = float(str(item.get("flu_rt", "0")).replace("+", "").strip())
                except (ValueError, TypeError):
                    continue
                if rate >= self.LEADER_THRESHOLD:
                    new_leading.add(theme_name)
                    leader_stock[theme_name] = (code, rate)

        self.leading_themes = new_leading
        for theme_name, (code, rate) in leader_stock.items():
            logger.info(
                f"🚀 [주도테마 판정] {theme_name} (대장주 {code} 등락률: {rate:.2f}%)"
            )
        logger.info(
            f"💾 [주도테마] 재계산 완료 (주도테마 {len(new_leading)}개 / "
            f"전체 {len(self.code_to_theme)}개 종목 매핑, 소요 {time.time()-start_time:.2f}초)"
        )

    def is_leading_theme_stock(self, stock_code: str) -> bool:
        """이 종목이 현재 주도테마 소속인지 O(1) 조회."""
        theme_name = self.code_to_theme.get(stock_code)
        if not theme_name:
            return False
        return theme_name in self.leading_themes

    def start_auto_update(self, interval_seconds: int = None):
        """백그라운드 스레드로 주기적 주도테마 재계산."""
        interval = interval_seconds or self.POLL_INTERVAL_SEC

        def _scheduler():
            logger.info(
                f"🚀 [주도테마 스케줄러] 백그라운드 갱신 활성화 (주기: {interval}초)"
            )
            while True:
                try:
                    time.sleep(interval)
                    self.update_leading_themes()
                except Exception as e:
                    logger.error(f"❌ [주도테마 스케줄러] 갱신 중 오류: {e}")

        updater_thread = threading.Thread(target=_scheduler, daemon=True)
        updater_thread.start()
