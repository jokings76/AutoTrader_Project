"""⚠️ 이름은 'Phase1B 컨트롤러'지만 **이건 전략이 아니라 데이터 파이프라인**이다.

[2026-08-02 현황 — 이름에 속지 말 것]
1B/1L 전략 코드는 이날 전부 삭제됐다. 그런데도 이 클래스가 남아 있는 이유는,
살아있는 두 전략(1A/Pullback)이 **여기 담긴 트래커로 진입 판정을 하기**
때문이다. 이 객체를 없애면 두 전략이 눈이 먼다.

  StrategyManager가 직접 참조하는 것 (전부 라이브):
    - self.trade_flow  : 체결틱 버퍼. 체결강도/거래대금/대량체결/최신가 —
                         틱 구동 진입(_maybe_tick_entry)의 '발사' 조건 판정
    - self.orderbook   : 매도 1~3호가 잔량 -> 하이브리드 주문(시장가/지정가) 판정
                         + 빈 호가창일 때 매도 N호가 지정가 산출
    - self.watched / start_watching / stop_watching / is_watching

[2026-08-02에 제거된 것]
  - WallDetector(매도벽 FSM) / ChemulEvaluator(5단계 FSM) 및 이들을 쓰던
    on_trade() / on_orderbook() / get_state() 래퍼.
    WallDetector 파라미터는 도입 시점부터 "TBD: 실데이터로 튜닝"이라고 적힌
    placeholder였고 한 번도 튜닝되지 않았다. 게다가 호가 잔량 이력은 어느
    공급자로도 과거 조회가 불가능해 **원리상 백테스트 검증이 영원히 불가능**하다.
    2026-07-31에 진입 판정에서 빠진 뒤로는 생성만 되고 아무 판단에도 관여하지
    않았다. 래퍼 3종은 StrategyManager가 트래커를 직접 갱신하는 구조라
    라이브에서 한 번도 호출된 적이 없다.
    복구가 필요하면 커밋 900757c 참고(core/strategy/wall_detector.py와
    chemul_evaluator.py 모듈 자체는 남아 있다).

TODO(다음 정리): 이 클래스를 TickDataPipeline 같은 이름으로 리네임.
  '1B'라는 이름이 실제로 사고를 두 번 냈다 — (1) 2026-08-01, "1B용 10:30
  정리"인 줄 알고 둔 코드가 1A의 틱 버퍼를 10초마다 지워 1A 시간창의 70%가
  죽어 있었다 (2) 2026-07-31, 1L 특별 케이스 누락으로 포지션이 매수 66초 만에
  조기청산됐다. 호출부가 ~40곳이라 실거래 검증이 끝난 뒤에 하는 편이 안전하다.
"""
from core.strategy.orderbook import OrderbookTracker
from core.strategy.trade_flow import TradeFlowTracker


class Phase1BController:
    """1A/Pullback의 체결틱·호가 데이터 파이프라인."""

    def __init__(self, history_window_sec: float = 120):
        self.orderbook = OrderbookTracker(history_window_sec=history_window_sec)
        self.trade_flow = TradeFlowTracker(max_window_sec=history_window_sec)

        # 감시 중인 종목 집합
        self.watched: set[str] = set()

    # ─── 감시 종목 관리 ────────────────────────
    def start_watching(self, stock_code: str):
        """진입 후보 감시 시작 — 이 종목의 체결틱/호가를 쌓기 시작한다."""
        self.watched.add(stock_code)

    def stop_watching(self, stock_code: str):
        """감시 중단 + 트래커 메모리 정리.

        ⚠️ 호출부는 강도 타이머(StrategyManager.reset_tick_entry_state)도
        같이 지워야 한다 — 버퍼만 비우고 타이머를 남기면 다시 감시가 켜졌을 때
        옛 타이머로 즉시 '무장'돼 진입 조건의 절반이 없는 채로 매수가 나간다
        (2026-08-02 통합 테스트에서 잡힌 결함).
        """
        self.watched.discard(stock_code)
        self.orderbook.reset(stock_code)
        self.trade_flow.reset(stock_code)

    def is_watching(self, stock_code: str) -> bool:
        return stock_code in self.watched
