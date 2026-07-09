"""
주문 관리자
─────────────────────────────────────
역할:
  - 매수/매도 결정 및 실행 (필터링 포함)
  - 포지션 추적 + 종목명 캐시
  - 손익 모니터링 → 익절/손절 자동 매도
  - 텔레그램 알림 (종목명 포함)

StrategyManager 통합:
  - StrategyManager가 자체 슬롯/청산 로직을 가지고 있으므로,
    `buy()` / `sell()` 단순 wrapper 메서드를 통해 호출.
  - 기존 try_buy / check_and_sell_positions / force_close_all은 호환을 위해 유지.
"""
import time
from datetime import datetime
from typing import Optional

from api.kiwoom_rest import KiwoomREST
from api.auth import send_telegram
from utils.logger import logger
from utils.price_helper import add_ticks, round_to_tick


# ─────────────────────────────────────
# 전략 파라미터
# ─────────────────────────────────────
BUY_AMOUNT_PER_STOCK = 2_000_000     # 종목당 매수금액 (200만원)
MAX_POSITIONS = 5                    # 동시 보유 최대 종목수
BUY_COOLDOWN_SEC = 300               # 같은 종목 재매수 쿨다운 (5분)

TAKE_PROFIT_PCT = 2.5                # +2.5% 익절
STOP_LOSS_PCT = -1.5                 # -1.5% 손절
TIME_STOP_MIN = 30                   # 30분 보유 후 0% 미만이면 정리

TRADING_START = "09:03"              # 거래 시작
TRADING_END = "15:10"                # 거래 종료 (이후 신규매수 X)
FORCE_CLOSE_TIME = "15:15"           # 강제 청산 시각

BUY_PRICE_OFFSET_TICKS = 1           # 현재가 +1틱 매수
SELL_PRICE_OFFSET_TICKS = -1         # 현재가 -1틱 매도


class OrderManager:
    def __init__(self, rest: KiwoomREST):
        self.rest = rest
        self.positions: dict[str, dict] = {}
        self.last_buy_ts: dict[str, float] = {}
        self.buying: set[str] = set()
        self._name_cache: dict[str, str] = {}
        self._force_close_done = False
        self._sell_failed: set[str] = set()

    # ─────────────────────────────────────
    # 종목명 헬퍼
    # ─────────────────────────────────────
    def get_stock_name(self, stock_code: str) -> str:
        if stock_code in self._name_cache:
            return self._name_cache[stock_code]
        info = self.rest.get_stock_info(stock_code)
        name = (info.get("stk_nm") or "").strip()
        if name:
            self._name_cache[stock_code] = name
        return name or "?"

    # ─────────────────────────────────────
    # 잔고 동기화
    # ─────────────────────────────────────
    def sync_positions_from_server(self):
        holdings = self.rest.get_holdings()
        new_positions = {}
        for code, info in holdings.items():
            if info["qty"] <= 0:
                continue
            new_positions[code] = {
                "qty": info["qty"],
                "avg_price": info["avg_price"],
                "name": info["name"] or self.get_stock_name(code),
                "bought_at": self.positions.get(code, {}).get("bought_at", time.time()),
            }
            if info["name"]:
                self._name_cache[code] = info["name"]
        self.positions = new_positions
        logger.info(f"📦 잔고 동기화: 보유 {len(new_positions)}종목")
        return new_positions

    # ─────────────────────────────────────
    # ★ StrategyManager용 단순 wrapper
    # ─────────────────────────────────────
    def buy(self, stock_code: str, qty: int, price: int = 0) -> dict:
        """단순 매수 wrapper (StrategyManager 호출용).

        Args:
            stock_code: 종목코드
            qty: 매수 수량
            price: 0이면 현재가 +1틱, >0이면 그 가격 지정가

        Returns:
            {"success": bool, "ord_no"?: str, "price"?: int, "error"?: str}
        """
        if qty <= 0:
            return {"success": False, "error": f"qty={qty}"}
        try:
            if price <= 0:
                cur = self.rest.get_current_price(stock_code)
                if cur <= 0:
                    return {"success": False, "error": "현재가 조회 실패"}
                price = round_to_tick(add_ticks(cur, BUY_PRICE_OFFSET_TICKS))

            result = self.rest.buy_market_order(
                stock_code, qty=qty, price=price, trde_tp="0"
            )
            rc = result.get("return_code")
            if rc != 0:
                return {"success": False, "error": result.get("return_msg", f"rc={rc}")}

            # 포지션 기록 (StrategyManager도 자체 holdings 관리하지만 sync 일관성 위해)
            name = self.get_stock_name(stock_code)
            self.positions[stock_code] = {
                "qty": qty,
                "avg_price": price,
                "bought_at": time.time(),
                "name": name,
                "ord_no": result.get("ord_no", ""),
            }
            self.last_buy_ts[stock_code] = time.time()

            return {
                "success": True,
                "ord_no": result.get("ord_no", ""),
                "price": price,
            }
        except Exception as e:
            logger.exception(f"[{stock_code}] buy() 예외")
            return {"success": False, "error": str(e)}

    def sell(self, stock_code: str, qty: int, price: int = 0) -> dict:
        """단순 매도 wrapper (StrategyManager 호출용).

        Args:
            stock_code: 종목코드
            qty: 매도 수량
            price: 0이면 현재가 -1틱, >0이면 그 가격 지정가

        Returns:
            {"success": bool, "ord_no"?: str, "price"?: int, "error"?: str}
        """
        if qty <= 0:
            return {"success": False, "error": f"qty={qty}"}
        try:
            if price <= 0:
                cur = self.rest.get_current_price(stock_code)
                if cur <= 0:
                    return {"success": False, "error": "현재가 조회 실패"}
                price = round_to_tick(add_ticks(cur, SELL_PRICE_OFFSET_TICKS))
                if price <= 0:
                    price = cur

            result = self.rest.sell_market_order(
                stock_code, qty=qty, price=price, trde_tp="0"
            )
            rc = result.get("return_code")
            if rc != 0:
                err_msg = result.get("return_msg", f"rc={rc}")
                # 영구 실패 판단
                if any(kw in str(err_msg) for kw in ["수량", "잔고", "보유", "체결", "부족"]):
                    self._sell_failed.add(stock_code)
                    self.positions.pop(stock_code, None)
                return {"success": False, "error": err_msg}

            self.positions.pop(stock_code, None)
            return {
                "success": True,
                "ord_no": result.get("ord_no", ""),
                "price": price,
            }
        except Exception as e:
            logger.exception(f"[{stock_code}] sell() 예외")
            return {"success": False, "error": str(e)}

    # ─────────────────────────────────────
    # 기존 매수 (호환용, ConditionManager 흐름 등에서 사용 가능)
    # ─────────────────────────────────────
    def try_buy(self, stock_code: str) -> bool:
        if not self._is_trading_time():
            logger.debug(f"[매수보류] {stock_code} - 거래시간 외")
            return False
        if stock_code in self.buying:
            return False
        if stock_code in self.positions:
            logger.debug(f"[매수보류] {stock_code} - 이미 보유")
            return False
        if len(self.positions) >= MAX_POSITIONS:
            logger.info(f"[매수보류] {stock_code} - 최대 보유 {MAX_POSITIONS}종목 도달")
            return False

        last = self.last_buy_ts.get(stock_code, 0)
        if time.time() - last < BUY_COOLDOWN_SEC:
            return False

        orderable = self.rest.get_orderable_amount()
        if orderable < BUY_AMOUNT_PER_STOCK:
            logger.warning(
                f"[매수보류] {stock_code} - 주문가능금액 부족 "
                f"(필요 {BUY_AMOUNT_PER_STOCK:,} / 가용 {orderable:,})"
            )
            return False

        self.buying.add(stock_code)
        try:
            return self._execute_buy_legacy(stock_code)
        finally:
            self.buying.discard(stock_code)

    def _execute_buy_legacy(self, stock_code: str) -> bool:
        cur_price = self.rest.get_current_price(stock_code)
        if cur_price <= 0:
            logger.warning(f"[매수실패] {stock_code} - 현재가 조회 실패")
            return False

        order_price = round_to_tick(add_ticks(cur_price, BUY_PRICE_OFFSET_TICKS))
        qty = BUY_AMOUNT_PER_STOCK // order_price
        if qty <= 0:
            logger.warning(f"[매수실패] {stock_code} - 수량 0 (price={order_price})")
            return False

        name = self.get_stock_name(stock_code)

        result = self.rest.buy_market_order(
            stock_code, qty=qty, price=order_price, trde_tp="0"
        )

        if result.get("return_code") != 0:
            msg = (f"❌ [매수실패] {name}({stock_code}) {qty}주 @ {order_price:,}원\n"
                   f"사유: {result.get('return_msg')}")
            logger.error(msg)
            send_telegram(msg, target="order")
            return False

        self.positions[stock_code] = {
            "qty": qty,
            "avg_price": order_price,
            "bought_at": time.time(),
            "name": name,
            "ord_no": result.get("ord_no", ""),
        }
        self.last_buy_ts[stock_code] = time.time()

        msg = (f"🚀 [매수주문] {name} ({stock_code})\n"
               f"수량: {qty}주\n"
               f"주문가: {order_price:,}원 (현재가 {cur_price:,}+{BUY_PRICE_OFFSET_TICKS}틱)\n"
               f"금액: {qty * order_price:,}원\n"
               f"ord_no: {result.get('ord_no')}")
        logger.info(msg)
        send_telegram(msg, target="order")
        return True

    # ─────────────────────────────────────
    # 매도 (자체 청산 로직, 현재 사용 안 함 — StrategyManager가 담당)
    # ─────────────────────────────────────
    def check_and_sell_positions(self):
        if not self.positions:
            return

        if self._should_force_close():
            self.force_close_all()
            return

        for stock_code in list(self.positions.keys()):
            if stock_code in self._sell_failed:
                continue
            try:
                self._evaluate_sell(stock_code)
            except Exception:
                logger.exception(f"포지션 평가 예외: {stock_code}")

    def _evaluate_sell(self, stock_code: str):
        pos = self.positions.get(stock_code)
        if not pos:
            return

        cur_price = self.rest.get_current_price(stock_code)
        if cur_price <= 0:
            return

        avg = pos["avg_price"]
        pnl_pct = (cur_price - avg) / avg * 100
        held_min = (time.time() - pos["bought_at"]) / 60

        if pnl_pct >= TAKE_PROFIT_PCT:
            self._execute_sell(stock_code, reason=f"익절 +{pnl_pct:.2f}%")
            return
        if pnl_pct <= STOP_LOSS_PCT:
            self._execute_sell(stock_code, reason=f"손절 {pnl_pct:.2f}%")
            return
        if held_min >= TIME_STOP_MIN and pnl_pct < 0:
            self._execute_sell(stock_code, reason=f"시간정리 {pnl_pct:.2f}% ({held_min:.0f}분)")
            return

    def _execute_sell(self, stock_code: str, reason: str = "") -> bool:
        pos = self.positions.get(stock_code)
        if not pos:
            return False

        cur_price = self.rest.get_current_price(stock_code)
        order_price = round_to_tick(add_ticks(cur_price, SELL_PRICE_OFFSET_TICKS))
        if order_price <= 0:
            order_price = cur_price

        name = pos.get("name") or self.get_stock_name(stock_code)

        result = self.rest.sell_market_order(
            stock_code, qty=pos["qty"], price=order_price, trde_tp="0"
        )

        if result.get("return_code") != 0:
            err_msg = str(result.get("return_msg", ""))
            msg = f"❌ [매도실패] {name}({stock_code})\n사유: {err_msg}"

            permanent_keywords = ["수량", "잔고", "보유", "체결", "부족"]
            if any(kw in err_msg for kw in permanent_keywords):
                self._sell_failed.add(stock_code)
                self.positions.pop(stock_code, None)
                msg += "\n→ 포지션에서 제거 (수동 확인 필요)"

            logger.error(msg)
            send_telegram(msg, target="order")
            return False

        pnl = (order_price - pos["avg_price"]) * pos["qty"]
        pnl_pct_actual = (order_price - pos["avg_price"]) / pos["avg_price"] * 100

        msg = (f"💰 [매도주문] {name} ({stock_code})\n"
               f"사유: {reason}\n"
               f"수량: {pos['qty']}주\n"
               f"주문가: {order_price:,}원\n"
               f"평균단가: {pos['avg_price']:,}원\n"
               f"예상손익: {pnl:+,}원 ({pnl_pct_actual:+.2f}%)")
        logger.info(msg)
        send_telegram(msg, target="order")

        self.positions.pop(stock_code, None)
        return True

    def force_close_all(self):
        if self._force_close_done:
            return
        self._force_close_done = True

        if not self.positions:
            return

        logger.info(f"🔔 장마감 강제청산: {len(self.positions)}종목")
        send_telegram(f"🔔 장마감 강제청산 시작: {len(self.positions)}종목", target="order")

        for stock_code in list(self.positions.keys()):
            self._execute_sell(stock_code, reason="장마감 강제청산")

    # ─────────────────────────────────────
    # 시간 체크
    # ─────────────────────────────────────
    @staticmethod
    def _is_trading_time() -> bool:
        now = datetime.now().strftime("%H:%M")
        return TRADING_START <= now <= TRADING_END

    @staticmethod
    def _should_force_close() -> bool:
        return datetime.now().strftime("%H:%M") >= FORCE_CLOSE_TIME

    # ─────────────────────────────────────
    # 상태 출력
    # ─────────────────────────────────────
    def status_summary(self) -> str:
        if not self.positions:
            return "보유 종목 없음"
        lines = [f"📊 보유 {len(self.positions)}종목"]
        for code, pos in self.positions.items():
            held = (time.time() - pos["bought_at"]) / 60
            name = pos.get("name", "?")
            lines.append(f"  {name}({code}): {pos['qty']}주 @ {pos['avg_price']:,}원 "
                         f"({held:.0f}분 보유)")
        return "\n".join(lines)