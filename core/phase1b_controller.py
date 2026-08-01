"""⚠️ 이름은 'Phase1B 컨트롤러'지만 **지금은 1A/Pullback의 데이터 파이프라인**이다.

[2026-08-02 현황 정리 — 이름에 속지 말 것]
1B 전략은 비활성(PHASE1B_ENABLED=False)이고 1L도 주석 처리됐다. 그래서 이
클래스의 역할은 "1B 매수 판정"이 아니라 **살아있는 두 전략(1A/Pullback)에
체결틱·호가 데이터를 공급하는 것**이다. 이 객체를 없애면 두 전략이 눈이 먼다.

  실제로 쓰이는 것 (StrategyManager가 **직접** 참조):
    - self.trade_flow  : 체결강도(FID228 아님, 자체 계산)/거래대금/대량체결/가격
    - self.orderbook   : 매도 1~3호가 잔량 -> 하이브리드 주문(시장가/지정가) 판정
    - self.watched / start_watching / stop_watching / is_watching

  정의만 있고 **라이브에서 한 번도 호출되지 않는 것**:
    - on_trade() / on_orderbook() / get_state()
      -> StrategyManager가 이 래퍼를 거치지 않고 트래커를 직접 갱신한다.
    - self.wall_detector (WallDetector) : 매도벽 FSM. 2026-07-31에 진입 판정에서
      완전히 제거됨(파라미터가 도입 이후 한 번도 튜닝된 적 없는 placeholder였고,
      호가 잔량 이력은 원리상 과거 검증이 불가능하다).
    - self.evaluator (ChemulEvaluator)  : 5단계 FSM. 위와 같은 이유로 미가동.
    -> 둘은 생성만 되고 아무 판단에도 관여하지 않는다(핫패스 비용 없음 —
       StrategyManager.on_orderbook에서 wall_detector 호출부가 주석 처리됨).
       PHASE1B_ENABLED를 되살릴 때를 위해 배선만 남겨둔 상태다.
"""
from typing import Optional

from core.strategy.orderbook import OrderbookTracker
from core.strategy.trade_flow import TradeFlowTracker
from core.strategy.wall_detector import WallDetector
from core.strategy.chemul_evaluator import ChemulEvaluator, ChemulState


class Phase1BController:
    """체결강도 전략의 데이터 파이프라인 + FSM 통합 래퍼."""

    def __init__(
        self,
        # 트래커 파라미터 (TBD: 실데이터로 튜닝)
        history_window_sec: float = 120,
        # WallDetector
        detect_multiplier: float = 5.0,
        shrink_ratio: float = 0.7,
        disappear_ratio: float = 0.2,
        avg_window_sec: float = 60,
        watch_levels: tuple = (1, 2),
        # ChemulEvaluator
        pullback_pct: float = -1.5,
        pullback_window_sec: float = 60,
        strength_short_window: float = 10,
        strength_long_window: float = 30,
        strength_min: float = 180,
        state_timeout_sec: float = 60,
    ):
        self.orderbook = OrderbookTracker(history_window_sec=history_window_sec)
        self.wall_detector = WallDetector(
            self.orderbook,
            detect_multiplier=detect_multiplier,
            shrink_ratio=shrink_ratio,
            disappear_ratio=disappear_ratio,
            avg_window_sec=avg_window_sec,
            watch_levels=watch_levels,
        )
        self.trade_flow = TradeFlowTracker(max_window_sec=history_window_sec)
        self.evaluator = ChemulEvaluator(
            trade_flow=self.trade_flow,
            wall_detector=self.wall_detector,
            orderbook=self.orderbook,
            pullback_pct=pullback_pct,
            pullback_window_sec=pullback_window_sec,
            strength_short_window=strength_short_window,
            strength_long_window=strength_long_window,
            strength_min=strength_min,
            state_timeout_sec=state_timeout_sec,
        )

        # 감시 중인 종목 집합
        self.watched: set[str] = set()

    # ─── 감시 종목 관리 ────────────────────────
    def start_watching(self, stock_code: str):
        """Phase 1B 후보 감시 시작."""
        self.watched.add(stock_code)

    def stop_watching(self, stock_code: str):
        """감시 중단 + 트래커 메모리 정리."""
        self.watched.discard(stock_code)
        self.evaluator.reset(stock_code)
        self.orderbook.reset(stock_code)
        self.trade_flow.reset(stock_code)
        self.wall_detector.reset(stock_code)

    def is_watching(self, stock_code: str) -> bool:
        return stock_code in self.watched

    def get_state(self, stock_code: str) -> ChemulState:
        return self.evaluator.get_state(stock_code)

    # ─── WS 콜백 진입점 ────────────────────────
    def on_trade(self, parsed_trade: dict, now: float = None) -> Optional[ChemulState]:
        """KiwoomWS on_trade에서 호출. 감시 종목이면 ChemulState 반환."""
        code = parsed_trade.get("stock_code")
        if not code or code not in self.watched:
            return None
        self.trade_flow.add_tick(
            code,
            parsed_trade.get("price", 0),
            parsed_trade.get("side", "neutral"),
            parsed_trade.get("volume", 0),
            now=now,
        )
        return self.evaluator.evaluate(code, now=now)

    def on_orderbook(self, parsed_orderbook: dict, now: float = None) -> Optional[ChemulState]:
        """KiwoomWS on_orderbook에서 호출."""
        code = parsed_orderbook.get("stock_code")
        if not code or code not in self.watched:
            return None
        self.orderbook.update(code, parsed_orderbook, now=now)
        self.wall_detector.on_orderbook(code, now=now)
        return self.evaluator.evaluate(code, now=now)