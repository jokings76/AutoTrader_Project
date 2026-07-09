"""Phase 1B 컨트롤러 — 체결강도 전략 데이터 파이프라인.

KiwoomWS의 on_trade / on_orderbook 콜백에서 호출.
내부 트래커들을 업데이트하고 ChemulEvaluator로 FSM 평가.
StrategyManager가 결과(ChemulState)를 받아 매수 실행 여부 결정.
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