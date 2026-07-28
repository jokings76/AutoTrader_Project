\# AutoTrader\_Project 작업 규칙



\## 기본 정보

\- 환경: 모의투자(IS\_MOCK), 왕복수수료 0.23%

\- 구조: asyncio + PostgreSQL (Docker `analytics\_pg` 컨테이너, 자동재시작 설정됨)

\- 사용자: 다윤, 한국어로 소통

\- 텔레그램 채널: 오토트레이더(order, 매수/매도 체결·실패), 주식 따릉이(signal, 시스템 로그/정기보고),

&#x20; 종가베팅 포착방(closing\_bet, 종가베팅 TOP10)



\## 작업 방식 (반드시 지킬 것)

1\. \*\*surgical 패치 우선\*\*: 파일 전체 재작성보다 "찾기 → 교체" 블록 단위로 제시. 

&#x20;  불가피하게 큰 함수 전체를 재구성해야 할 때만 함수 단위 통째 교체.

2\. \*\*파일 삭제 금지\*\*: 기존 파일은 지우지 않고 `\_legacy` 접미사로 백업 후 교체.

3\. \*\*검증 필수\*\*: 코드 변경 직후 반드시 `python -c "import \[모듈]; print('OK')"`로 

&#x20;  import 성공 여부 확인. 여러 파일을 고쳤으면 각각 검증.

4\. \*\*큰 변경 전 드라이런 권고\*\*: 특히 매매 로직(진입/청산/점수 계산) 변경은 

&#x20;  장 시작 전이나 소규모 테스트 권장.

5\. \*\*비동기 코드 주의\*\*: 메인 루프를 막는 `sleep` 절대 금지 (WS 연결 끊김 원인이었던 적 있음).

&#x20;  `asyncio.sleep`만 사용.

6\. \*\*인덴트 안전\*\*: Python 들여쓰기가 걸린 수정은 특히 신중하게 — 

&#x20;  부분 문자열 교체보다 함수/블록 전체를 정확히 교체하는 방식을 우선 고려.

7\. \*\*수정 후 백업 병행\*\* (2026-07-28): 그날 코드 수정이 어느 정도 쌓이면(세션

&#x20;  마무리 시점 등) git 커밋 + 날짜별 파일 스냅샷 백업을 같이 남긴다.

&#x20;  - git: 의미 단위로 커밋(민감정보 커밋 전 확인 — config.ini/settings.py/.env는

&#x20;    이미 .gitignore 처리됨, 그 외 파일에 하드코딩된 키/비밀번호 없는지 훑을 것).

&#x20;  - 파일 스냅샷: 프로젝트 폴더 밖 `AutoTrader\_Project\_backups\YYYY-MM-DD\`에

&#x20;    전체 복사(.git, \_\_pycache\_\_ 제외). Windows 작업 스케줄러에도 매일 자동

&#x20;    실행 등록(대화 세션과 무관하게 돌아감) — 스크립트는

&#x20;    `C:\Users\rober\OneDrive\문서\vscode\daily\_backup.ps1`.



\## 히스토리 (참고용, 과거 세션 요약)

\- 2026-07-24: Phase3 A/B 트리거 재설계, 손절/익절 정책 분리, 눌림목 VWAP 필터,

&#x20; 자동실행 인프라 버그 3종 해결, 안전종료 기능

\- 2026-07-26: VWAP 필터 5종 게이트(ATR 적응형/기울기/reclaim/밴드/연속스코어) 확장,

&#x20; 네이버 테마 종목코드 크롤러(fetch\_themes.py), 거래대금 폭발 히스토리 파이프라인

&#x20; (core/history\_fetcher.py, core/explosion\_scorer.py), 종가베팅 독립 스케줄러

&#x20; (main.py task\_closing\_bet\_scanner, 매일 14:50), 텔레그램 채널 3분리(order/signal/closing\_bet),

&#x20; 수동매도 즉시 감지+슬롯 해제(\_reconcile\_manual\_sells, SYNC\_INTERVAL 60→15초),

&#x20; 슬롯 교체 로직(core/slot\_replacement.py, 체결강도 하락+거래량 정체 종목을

&#x20; 감시종목 고득점 후보로 교체, 손절과 동일 취급, 하루 40회 상한)

\- 2026-07-27: 치명적 라이브 버그 다수 수정 — on\_price\_update()에 \_execute\_buy 알림

&#x20; 코드가 잘못 붙어있어 손절/트레일링/익절 판정이 전혀 실행 안 되던 문제,

&#x20; \_net\_profit()이 존재하지 않는 전역변수 참조로 매도할 때마다 크래시 나던 문제

&#x20; (→ "매도됐는데 계속 보유중" 증상의 원인), entry\_reason DB 컬럼 길이초과로

&#x20; 매수기록 실패 시 포지션 추적 끊기던 문제. 테마 매핑 REST 호출 800회→2회 축소

&#x20; (ka10027 랭킹 API 활용). 지수 방어 로직을 코스피 전용에서 코스피+코스닥

&#x20; worst-of-two(NORMAL/CAUTION -3%/HALT -5%)로 통합, 모든 진입 전략에 일관 적용.

&#x20; 일일 백테스트 시스템 신규(core/daily\_backtest.py, 매일 15:30 자동실행 →

&#x20; 텔레그램 오토트레이더 전송, task\_auto\_shutdown 15:20 자동종료는 이와 충돌해

&#x20; 임시 비활성화). 네트워크 단절/복구 3단계 방어 완성(on_disconnect/on_reconnect

&#x20; 콜백, 단절시간대별 격리기간 10초/60초/10분 차등, 미체결 주문 재확인 후 pending

&#x20; 정리·무조건취소 안 함, WS backoff에 jitter 추가).

- 2026-07-27 (계속) — 전략 구조 전면 재설계: 기존 1A/1S/1B/2/3/1N 체제에서
  **1A + Pullback + 1B(FSM) + 1L(주도주) 4개 전략 체제**로 개편.
  - 1A: 30봉신고가 필터 → 거래량증가지속(`is_volume_increasing_streak`)으로 교체
    (이 교체 과정에서 `calc_ma.is_30_candle_new_high` 호출이 함수를 모듈처럼 잘못
    참조해 매번 AttributeError로 조용히 실패하던 버그 발견·해결 — 1A는 사실상
    한 번도 작동한 적 없었음). 시간대 09:01~10:30(기본 6.5점)/10:30~14:50(8.5점
    상향, CAUTION 시 +1.0 추가). 슬롯 3개.
  - Pullback: 09:01~10:30로 시간대 변경(기존 09:30~10:40), 전용 슬롯 3개
    분리("1A_눌림" 라벨, 기존엔 1A와 슬롯 공유). 매수 미체결 시 1B 감시 시작
    트리거로 지정(기존엔 Surge가 담당).
  - 1B(5단계 FSM): Pullback 쪽으로 트리거 이관, 슬롯 4→3개.
  - 1L(주도주): 테마+체결강도100 판정을 순간체크에서 **2분 연속 유지**로 변경
    (`_leading_since` 타이머), 시간대 09:01~10:50 추가, 슬롯 2→3개.
  - 슬롯 구조: 1A/Pullback/1B/1L 각각 자체 상한 3개 + `MAX_HOLDINGS=6`으로 전체
    합산 상한 공유 — 1L/1B는 실시간 틱 콜백이라 사실상 우선 처리, 1A/Pullback은
    조건검색 경로라 자연스럽게 차순위.
  - **삭제(`_legacy` 백업)**: 1S(Surge), "2"(Phase2 — `evaluate_phase2` 정의만
    있고 호출부가 아예 없어 원래도 죽어있었음), 3(Phase3 A/B, `Phase3Controller`
    포함), 1N(10MA 눌림목 — `watch_candidates` 큐에 아무도 값을 넣지 않아 원래도
    죽어있었음). `entries/` 패키지(registry/base/surge/pullback/phase3) 전체
    미사용 처리, `on_condition_hit` 3중 루프를 단순 흐름으로 재작성.
  - 청산: 트레일링은 1L 전용, 나머지(1A/Pullback/1B)는 항상 flat 익절 2.5%
    (기존 09:30 매수시각컷/Phase3 A·B 트리거 기반 분기 전부 제거).
  - `config/phase_settings.py`: MAX_HOLDINGS 5→6, PHASE_1B 4→3, PHASE_2/PHASE_3/
    SCORING 삭제. `core/order_manager.py`의 MAX_POSITIONS도 5→6(실매수 경로엔
    안 걸리지만 일관성 위해). `core/daily_backtest.py`도 새 전략셋(1A/Pullback만
    재현)에 맞게 갱신.
- 2026-07-27 (계속) — 실거래 크래시 진단 및 수정:
  - **의도치 않은 차단 알림**: 지수방어 모드 전환(NORMAL↔CAUTION↔HALT) 시에만
    텔레그램 알림 추가(반복 호출 스팸 방지, 상태 바뀔 때만 발송).
  - **일부 수동매도 반영**: `_reconcile_manual_sells`가 전량매도(서버 잔고에서
    종목 자체가 사라짐)만 감지하던 걸, 일부 수량만 줄어든 경우도 감지해서
    `holdings[code]["qty"]`를 서버 값으로 갱신하도록 확장(기존엔 죽은 코드였던
    `pos.get("qty", ...)` 폴백이 이제 실제로 쓰임).
  - **작업 스케줄러 진단**: `AutoTrader_Start` 작업이 매일 09:01 정상 실행되는
    것 확인했으나, 그날은 테마 800회 호출 버그(이미 해결됨)로 멈춰서 실매매
    전 단계에서 죽고 있었던 것으로 확인. `C:\AutoTrader_Bot\start_trader.bat`
    (실제 스케줄러가 가리키는 파일, 관리자 권한 없어 스케줄러 자체는 못 고침)를
    프로젝트 폴더의 `start_trader.bat`를 호출하도록 위임 처리해서 경로 이원화
    해소.
  - **치명적 크래시 원인 발견·수정**: `task_closing_bet_scanner`가 코루틴
    시작 즉시(시간대 체크보다 먼저, try/except 밖에서) `from core.explosion_scorer
    import evaluate_closing_bet_candidate`를 실행하는데 이 함수가 애초에
    구현된 적이 없어서 **봇 기동 시마다 100% ImportError로 `asyncio.gather()`
    전체가 죽는 상태**였음(실제 야간 테스트에서 재현·확인). import를 기존
    try/except 안으로 이동시켜 이 기능만 조용히 스킵되도록 수정, 같은 위험
    패턴이던 `task_daily_backtest`/`task_slot_replacement`의 import도 예방
    차원에서 같이 옮김. `evaluate_closing_bet_candidate` 자체는 여전히
    미구현 — 설계만 합의됨(아래 "다음 할 일" 참고), 구현은 다음 세션으로 이월.
  - **start_trader.bat/stop_trader.bat 한글 인코딩 문제**: cmd.exe가 UTF-8로
    저장된 .bat 파일을 기본 cp949로 잘못 해석해 콘솔에 깨진 글자 출력 + 일부
    환경(바로가기 등)에서 명령 파싱 자체가 깨짐. `chcp 65001`로는 완전히
    해결 안 돼서, **.bat 파일 안의 한글 텍스트를 전부 제거**하고 텔레그램
    알림 문구는 별도 파일 `notify_scheduler_start.py`(파이썬이 UTF-8 소스를
    안정적으로 읽음)로 분리하는 방식으로 최종 해결. `stop_trader.bat` 신규
    생성(STOP_SIGNAL 파일 생성 → main.py의 기존 감시 로직이 5초 내 안전 종료).
  - ~~남은 원인불명 잔여 이슈: 배치파일 실행 시 "지정된 경로를 찾을 수 없습니다"~~
    → **2026-07-28에 근본원인 확정·해결** (아래 히스토리 참고).
- 2026-07-28 — **스케줄러 09:01 자동실행 근본 수정**: 아침에 python 전체경로
  하드코딩으로 1차 수정했다고 봤으나, 실제 `schtasks /Run`으로 재검증하니 여전히
  100% 실패(LastTaskResult=1, 로그 완전 무출력). 여러 단계로 원인 추적:
  (1) `AutoTrader_Start` 작업 자체가 2026-07-22에 Task Scheduler GUI로 수동
  등록된 것으로 추정, `schtasks /Create`로 새로 만든 진단용 작업들은 전부 정상
  작동 → 기존 작업 등록 상태 자체가 문제로 판명, `schtasks /Delete` + `/Create`로
  재등록(PowerShell `Register-ScheduledTask` cmdlet은 이 세션 권한으로 "Access is
  denied" — `schtasks.exe` CLI는 됨, 앞으로 스케줄 작업 등록/변경은 이 방법 사용).
  (2) 재등록 후에도 "'o'은(는) 내부 또는 외부 명령이 아닙니다" 에러 지속 →
  순수 ASCII 배치파일은 스케줄러에서 완벽히 작동하는 것 확인 후, 문제를
  `C:\Users\...\OneDrive\문서\...` 경로의 **"문서"(한글) 폴더명 자체**로 좁힘 —
  cmd.exe가 이 UTF-8 경로 문자열을 스케줄러의 S4U/비대화형 실행 컨텍스트에서
  잘못된 코드페이지로 읽어 파싱이 깨짐(2026-07-27에 "찾은" 것과 같은 뿌리의
  문제였음, 그때는 배치파일 자체 텍스트만 정리하고 "문서" 경로 문자열 자체는
  못 건드려서 미해결로 남았던 것). 8.3 short path는 이 볼륨/폴더에서 비활성화돼
  있어 우회 불가 확인. **최종 해결**: `C:\AutoTrader_Bot\ProjectRoot`에 디렉토리
  junction 생성(`New-Item -ItemType Junction`, 관리자 권한 불필요)해서 실제
  프로젝트 폴더를 한글 없는 경로로 매핑, `C:\AutoTrader_Bot\start_trader.bat`와
  프로젝트의 `start_trader.bat` 둘 다 이 junction 경로를 참조하도록 수정.
  `schtasks /Run`으로 실제 스케줄러 컨텍스트에서 재현 테스트 → 성공(봇 정상
  기동, LastTaskResult=0) 확인. **앞으로 스케줄러(비대화형/S4U 컨텍스트)에서
  실행되는 모든 배치파일/스크립트는 "문서" 등 한글이 섞인 실제 경로 대신
  `C:\AutoTrader_Bot\ProjectRoot` junction 경로를 참조할 것** — 인터랙티브
  세션에서는 문제없이 작동해서 이 클래스의 버그는 수동 테스트로는 절대
  못 잡음, 항상 `schtasks /Run`으로 실제 스케줄러 컨텍스트에서 검증 필요.



\## 다음 할 일 (2026-07-28 기준 갱신)

\### 내일(07-29) 아침 확인할 것
1. 09:01 스케줄러 자동실행 정식 검증 — 어제 junction 우회로 수동 재현은 성공
   확인(`schtasks /Run`, LastTaskResult=0), 하지만 실제 예약 트리거로는 아직
   미검증. 봇 로그 첫 줄이 09:01대에 찍히는지 확인.
2. 14:50 종가베팅 스캐너 실전 첫 검증 — `evaluate_closing_bet_candidate` 구현
   완료(2026-07-28) + 이력조회를 14:50 시점으로 이관 완료. 실제 후보가 나오는지,
   `bullish_ratio>=0.5` 임계값이 너무 빡빡/헐거운지 확인.
3. 매수 텔레그램 알림 새 포맷(조건검색식/매수이유/판단근거) 실전 가독성 확인.
4. 지수 급락(-5%) 대응 익절정책(flat 1.5%, 11시 이후 신규매수 중단) — 아직 실전
   미발동, 조건 충족 시 정상 작동하는지 확인.
5. watchlist 재진입 로직(1A/Pullback 슬롯 대기 후 즉시매수) 실전 첫 검증 —
   로그에 "watchlist 재진입 매수 성공" 나오는지.

\### 미해결/관찰 필요
6. WS 재연결 핸들러(`_on_ws_reconnet`) 미작동 원인 미확정 — 진단 로그 심어둠
   (`kiwoom_ws.py` "재연결 콜백 판정" 로그), 다음 실제 단절 시 원인 확정 필요.
7. 부분 매도(일부 수동매도) 시 DB `buy_quantity` 미반영 — 전량매도는 고쳤지만
   부분매도는 재시작 시 원래 수량으로 복원되는 문제 남음(심각도 낮음).
8. TradeFlowTracker 관련 버그 2건은 수정 완료, 실전 1A 체결강도지속 체크가
   기대대로 작동하는지 관찰.
9. theme_manager.py의 save_themes_to_db() DB 저장 실패("the query contains
   more than one '%s' placeholder") — execute_values 사용법 오류로 추정,
   매매엔 영향 없지만 테마 데이터가 DB에 계속 안 남고 있음. 미착수.
10. task_auto_shutdown(15:20 자동종료) 재활성화 여부 미결정 — daily_backtest와
    순서 충돌로 여전히 비활성화 상태.
11. 손절 종목 당일 재매수 차단 정책은 현행 유지(재검토 안 함, 의도된 설계).

\### 2026-07-28 세션 요약 (상세는 위 히스토리 항목 참고)
reconcile race/DB 스키마 2건/로거 단절/kiwoom_rest 블로킹/수동매도 DB 미반영/
1B 무기한대기/TradeFlowTracker 미구현/`_throttle` 레이스컨디션/스케줄러 한글경로
전부 수정 완료. 신규: 매수등락률 12%상한, 지수급락 대응 익절정책, watchlist
재진입, 종가베팅 스캐너 구현+이관, 매수알림 개선, 매일 git커밋+파일백업 관례
확립(작업방식 7번, `daily_backup.ps1` 매일 18:00 자동실행). 바탕화면에
"AutoTrader 시작/종료" 바로가기 생성(junction 경로 사용).

