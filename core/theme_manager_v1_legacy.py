# ══════════════════════════════════════════════════════════════════
# ⚠️ [삭제 예정 / DEPRECATED] — 2026-08-02 표시
#
# 이 파일은 **어디서도 import되지 않는 폐기된 코드**다.
# (main.py 기준 도달성 분석으로 확인 — 라이브 모듈 34개에 포함되지 않음)
#
# 지금 살아있는 전략은 **1A / Pullback 두 개뿐**이고, 둘 다 틱 구동
# (체결강도 FID228 3초 연속 + 대량체결 버스트)으로 동작한다.
# 아래 코드는 그 이전 설계(Phase2/Phase3/Surge/WallDetector FSM 등)의
# 잔재이므로 **현재 동작의 근거로 삼으면 안 된다.**
#
# 남겨둔 이유: CLAUDE.md 작업규칙 2("파일 삭제 금지 — _legacy로 보존").
# 삭제해도 git 히스토리로 언제든 복구 가능하므로, 다윤님이 판단해서
# 정리하면 된다. 정리 시 이 배너가 붙은 파일 전체가 대상이다.
#   확인:  git ls-files "*_legacy.py"
#   삭제:  git rm $(git ls-files "*_legacy.py")
# ══════════════════════════════════════════════════════════════════

import time
import threading
import requests
from datetime import date
import psycopg2.extras

# 프로젝트 공용 로거
from utils.logger import logger
from api.kiwoom_api import get_program_net_buy
from db.connection import get_cursor


class ThemeManager:
    """깃허브에서 주도 테마를 가져와서 종목이 주도 테마인지 판별하는 클래스"""

    def __init__(self):
        self.themes = []  # 리스트 형태 저장
        self.code_to_theme = {}  # {'005930': 'AI반도체'} 형태 매핑
        self.last_update = 0.0
        # 💡 [최적화] 종목별 프로그램 순매수 캐시 {'종목코드': {'net_buy': 12000, 'updated_at': timestamp}}
        self.program_cache = {}

    def fetch_themes_from_github(self):
        url = "https://raw.githubusercontent.com/jokings76/AutoTrader_Project/main/theme_data.json"
        try:
            response = requests.get(url, timeout=10)
            print(f"디버깅 - 서버 응답 내용: {response.text}")
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

                    if code_list:
                        for code in code_list:
                            clean_code = str(code).lstrip("A")
                            if clean_code.isdigit() and len(clean_code) == 6:
                                self.code_to_theme[clean_code] = theme.get(
                                    "name", "알 수 없음"
                                )

                self.last_update = time.time()
                self.save_themes_to_db()
                logger.info(
                    f"✅ 주도테마 {len(self.themes)}개 로드완료 (종목 수: {len(self.code_to_theme)}개)"
                )

                # 테마 정보를 새로 로드한 직후, 캐시를 즉시 한 번 채워줍니다.
                self.update_program_cache()

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

    def update_program_cache(self):
        """
        💡 [신설] 등록된 모든 테마 소속 종목의 프로그램 수급을 일괄 갱신하여 캐싱합니다.
        장중 메인 루프나 백그라운드 쓰레드에서 주기적(예: 1분~2분)으로 호출해 주어야 합니다.
        """
        if not self.code_to_theme:
            return

        logger.info("🔄 [수급 캐시] 전체 테마 종목 프로그램 순매수 갱신 시작...")
        start_time = time.time()

        # 중복 요청 방지를 위해 set 처리 후 리스트화
        all_codes = list(set(self.code_to_theme.keys()))

        for code in all_codes:
            # 실시간 프로그램 수급 API 호출
            try:
                net_buy = get_program_net_buy(code)
                # 메모리 캐시에 저장 (조회 시간 기록)
                self.program_cache[code] = {
                    "net_buy": net_buy,
                    "updated_at": time.time(),
                }
            except Exception as e:
                logger.error(f"❌ [{code}] 수급 데이터 갱신 실패: {e}")

            # 키움 API 서버 과부하 방지 및 안정적인 조회를 위한 미세한 슬립 (50ms)
            time.sleep(0.05)

        logger.info(
            f"💾 [수급 캐시] {len(all_codes)}개 종목 수급 캐싱 완료 (소요시간: {time.time() - start_time:.2f}초)"
        )

    def _is_in_leading_theme(
        self, theme_stocks_data, leader_threshold=15.0, program_threshold=50000
    ):
        """
        대장주 모멘텀을 필수로 확인하고, 캐싱된 실시간 프로그램 수급 데이터를 활용해 딜레이 없이 주도 테마를 판별합니다.
        """
        if not theme_stocks_data:
            return False, 0

        # 1. [필수 조건] 대장주 모멘텀 확인
        max_return = max(stock.get("등락률", 0) for stock in theme_stocks_data)
        if max_return < leader_threshold:
            return False, 0

        theme_score = 50

        # 2. [수급 조건] 💡 API를 호출하지 않고, 메모리에 캐싱된 데이터에서 즉시 꺼내와 합산 (속도 O(1))
        total_program_net_buy = 0
        for stock in theme_stocks_data:
            code = stock.get("종목코드")
            if code:
                # 캐시에 데이터가 있으면 가져오고, 없으면 아직 갱신 전이므로 0 처리
                stock_cache = self.program_cache.get(code, {})
                realtime_program = stock_cache.get("net_buy", 0)
                total_program_net_buy += realtime_program

        # 3. 프로그램 순매수 규모에 따른 가점/감점 반영
        if total_program_net_buy >= program_threshold * 3:
            theme_score += 40
        elif total_program_net_buy >= program_threshold:
            theme_score += 20
        elif total_program_net_buy < 0:
            theme_score -= 15

        is_leading = theme_score >= 70

        logger.info(
            f"[주도테마 판별] 대장주 최고등락률: {max_return}%, "
            f"프로그램 총순매수(캐시): {total_program_net_buy:,}주 -> 최종 점수: {theme_score}점 (진입여부: {is_leading})"
        )

        return is_leading, theme_score

    def start_auto_update(self, interval_seconds=120):
        """
        💡 [최적화] 백그라운드 스레드를 생성하여 지정된 주기(기본 2분)마다 수급 캐시를 일괄 자동 갱신합니다.
        메인 루프의 멈춤 현상(Blocking)을 완벽하게 방지합니다.
        """

        def _scheduler():
            logger.info(
                f"🚀 [수급 스케줄러] 백그라운드 자동 갱신 스레드가 활성화되었습니다. (주기: {interval_seconds}초)"
            )
            # 프로그램 시작 시 최초 1회 즉시 갱신은 fetch_themes_from_github 등에서 수행하므로,
            # 여기서는 주기적인 대기 후 반복 실행합니다.
            while True:
                try:
                    time.sleep(interval_seconds)
                    self.update_program_cache()
                except Exception as e:
                    logger.error(
                        f"❌ [수급 스케줄러] 백그라운드 갱신 중 오류 발생: {e}"
                    )

        # 메인 프로세스가 종료되면 같이 종료되도록 daemon=True 설정
        updater_thread = threading.Thread(target=_scheduler, daemon=True)
        updater_thread.start()
