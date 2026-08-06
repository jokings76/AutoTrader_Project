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
# 종목당 기본 매수금액.
# 2026-08-04 실전 전환: 200만원 -> 50만원(모의 대비 1/4, 사용자 지정).
# 2026-08-05 장마감 후 사용자 지정: 50만원 -> **200만원** 환원.
# 실전 예수금 약 1,080만원(08-05 실측 d2 기준) 대비 노출:
#   평상시 슬롯 6 x 200만 x weight 0.90 = 약 1,080만원 (예수금의 ~100%)
#   확장 슬롯 8 + tier 최대 1.5배(PHASE1A_SIZE_MAX_MULT)면 이론상 예수금을 넘는다.
# ⚠️ 초과분은 키움이 주문을 거부할 뿐 오주문은 아니지만, 50만원 때와 달리
#    **예수금이 실질적인 슬롯 상한으로 작동**한다는 점이 달라졌다.
#    실측 동시보유는 최대 2/6(08-03)이라 통상은 도달하지 않는다.
# ⚠️ 슬롯 개수·tier 배수·MDD 한도는 **모의와 동일하게 유지**한다(사용자 지정).
BUY_AMOUNT_PER_STOCK = 1_000_000
MAX_POSITIONS = 8  # 동시 보유 최대 종목수 (strategy_manager.MAX_HOLDINGS_HARD와 일치,
                   # 2026-07-31 확장 슬롯 도입으로 6->8. 평상시 상한 6은 전략 쪽에서 관리)
BUY_COOLDOWN_SEC = 300  # 같은 종목 재매수 쿨다운 (5분)

TAKE_PROFIT_PCT = 2.5  # +2.5% 익절
STOP_LOSS_PCT = -1.5  # -1.5% 손절
TIME_STOP_MIN = 30  # 30분 보유 후 0% 미만이면 정리

TRADING_START = "09:03"  # 거래 시작
TRADING_END = "15:10"  # 거래 종료 (이후 신규매수 X)
# 강제 청산 시각 (2026-08-01 사용자 지정으로 15:15 -> 15:10).
# 진입은 14:50(1A/Pullback 공통)에 끝나고, 남은 보유분은 여기서 전량 청산한다.
# ENTRY_HARD_CUTOFF(15:10)와 같은 시각이라 "청산 중에 새로 사는" 겹침이 없다.
FORCE_CLOSE_TIME = "15:10"

BUY_PRICE_OFFSET_TICKS = 1  # 현재가 +1틱 매수
SELL_PRICE_OFFSET_TICKS = -1  # 현재가 -1틱 매도


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
                "sizing": self.positions.get(code, {}).get("sizing", "REGULAR_VOLUME"),
                "exit_strategy": self.positions.get(code, {}).get(
                    "exit_strategy", "REGULAR"
                ),
            }
            if info["name"]:
                self._name_cache[code] = info["name"]
        self.positions = new_positions
        logger.info(f"📦 잔고 동기화: 보유 {len(new_positions)}종목")
        return new_positions

    # ─────────────────────────────────────
    # ★ StrategyManager용 단순 wrapper (확장형)
    # ─────────────────────────────────────
    def buy(
        self,
        stock_code: str,
        qty: int,
        price: int = 0,
        sizing: str = "REGULAR",
        exit_strategy: str = "REGULAR",
        order_style: str = "limit",
        ref_price: int = 0,
    ) -> dict:
        """단순 매수 wrapper (StrategyManager 호출용).

        Args:
            stock_code: 종목코드
            qty: 매수 수량
            price: 0이면 현재가 +1틱, >0이면 그 가격 지정가 (order_style="limit"일 때만 의미 있음)
            sizing: "MAX_VOLUME" | "REGULAR_VOLUME" | "MIN_VOLUME"
            exit_strategy: "REGULAR" | "TRAILING_EXIT" (특수 청산 필요시 사용)
            order_style: "limit"(지정가, trde_tp=0) | "market"(시장가, trde_tp=3)
                (2026-08-01 신규) 호가창 두께에 따라 호출부가 선택한다 —
                매도 1~3호가가 두툼하면 시장가가 유리하고, 텅 비어 있으면
                시장가는 위쪽 호가를 훑어 올라가므로 지정가로 간다.
            ref_price: 시장가 주문 시 '예상 체결가'로 기록할 기준가(보통 매도1호가).
                주문 자체에는 안 쓰이고 반환값 price(=DB/손익 계산 기준)에만 쓴다.
                0이면 REST로 현재가를 1회 조회해 채운다.

        Returns:
            {"success": bool, "ord_no"?: str, "price"?: int, "style"?: str, "error"?: str}
            price는 지정가면 주문가, 시장가면 '예상 체결가'다 — 호출부는 이 값을
            매수단가로 기록해야 실제 체결가와의 괴리가 최소가 된다.
        """
        if qty <= 0:
            return {"success": False, "error": f"qty={qty}"}
        style = "market" if str(order_style).lower() == "market" else "limit"
        try:
            if style == "market":
                # 기록용 기준가만 확보 — 실패해도 주문 자체는 낼 수 있지만,
                # 매수단가를 모르면 손절/익절 판정이 통째로 무의미해지므로
                # 기준가를 못 구하면 주문을 내지 않는다(보수적).
                expected = int(ref_price or 0)
                if expected <= 0:
                    expected = self.rest.get_current_price(stock_code)
                if expected <= 0:
                    return {"success": False, "error": "시장가 기준가 조회 실패"}
                result = self.rest.buy_market_order(
                    stock_code, qty=qty, price=0, trde_tp="3"
                )
                fill_price = expected
            else:
                if price <= 0:
                    cur = self.rest.get_current_price(stock_code)
                    if cur <= 0:
                        return {"success": False, "error": "현재가 조회 실패"}
                    price = round_to_tick(add_ticks(cur, BUY_PRICE_OFFSET_TICKS))
                result = self.rest.buy_market_order(
                    stock_code, qty=qty, price=price, trde_tp="0"
                )
                fill_price = price

            rc = result.get("return_code")
            if rc != 0:
                return {"success": False, "error": result.get("return_msg", f"rc={rc}")}

            name = self.get_stock_name(stock_code)
            self.positions[stock_code] = {
                "qty": qty,
                "avg_price": fill_price,
                "bought_at": time.time(),
                "name": name,
                "ord_no": result.get("ord_no", ""),
                "sizing": sizing,
                "exit_strategy": exit_strategy,
                "order_style": style,
            }
            self.last_buy_ts[stock_code] = time.time()

            return {
                "success": True,
                "ord_no": result.get("ord_no", ""),
                "price": fill_price,
                "style": style,
            }
        except Exception as e:
            logger.exception(f"[{stock_code}] buy() 예외")
            return {"success": False, "error": str(e)}

    def sell(self, stock_code: str, qty: int, price: int = 0,
             order_style: str = "market") -> dict:
        """단순 매도 wrapper (StrategyManager 호출용).

        Args:
            stock_code: 종목코드
            qty: 매도 수량
            price: order_style="limit"일 때만 의미. 0이면 현재가 -1틱.
            order_style: **기본값이 "market"(시장가)** — 2026-08-01 변경.

        왜 매도를 시장가로 바꿨나 (중요):
          기존엔 모든 매도가 `현재가 -1틱` 지정가였다. 그런데 키움 응답의
          `return_code == 0`은 "주문 접수 성공"이지 "체결"이 아닌데,
          StrategyManager._execute_sell은 접수를 체결로 간주해서 holdings에서
          제거하고 DB에 매도가를 기록해버린다. 미체결이면 그 포지션은
          **봇의 관리 대상에서 영구 이탈**한다 — _reconcile_manual_sells는
          holdings->서버 방향만 보고, 서버에는 있는데 holdings엔 없는 종목을
          되살리는 로직이 없기 때문이다. 손절도 익절도 안 되고 15:15 강제청산
          (같은 지정가 경로)마저 미체결이면 오버나이트로 넘어간다.
          미체결 확률 자체는 낮지만 위험 구간이 하필 급락 손절이다:
          get_current_price(REST) 조회와 주문 도달 사이에 가격이 더 떨어지면
          지정가가 시장보다 위에 남는데, 429 스로틀 시 그 사이에 2초 blocking
          sleep이 낀다. -3% 손절이 -6%가 되는 대가로 1틱을 아끼는 거래다.
          이미 "판다"고 결정한 뒤이므로 체결 확실성이 우선이다.

        Returns:
            {"success": bool, "ord_no"?: str, "price"?: int, "style"?: str, "error"?: str}
        """
        if qty <= 0:
            return {"success": False, "error": f"qty={qty}"}
        style = "limit" if str(order_style).lower() == "limit" else "market"
        try:
            if style == "market":
                result = self.rest.sell_market_order(
                    stock_code, qty=qty, price=0, trde_tp="3"
                )
            else:
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
                if any(
                    kw in str(err_msg)
                    for kw in ["수량", "잔고", "보유", "체결", "부족"]
                ):
                    self._sell_failed.add(stock_code)
                    self.positions.pop(stock_code, None)
                return {"success": False, "error": err_msg}

            self.positions.pop(stock_code, None)
            return {
                "success": True,
                "ord_no": result.get("ord_no", ""),
                "price": price,      # 시장가면 0 (체결가는 호출부가 아는 현재가로 기록)
                "style": style,
            }
        except Exception as e:
            logger.exception(f"[{stock_code}] sell() 예외")
            return {"success": False, "error": str(e)}

    # ========================================
    # 직접 매수 (ConditionManager 및 내부 호출용)
    # ========================================
    def try_buy(
        self,
        stock_code: str,
        sizing: str = "REGULAR_VOLUME",
        exit_strategy: str = "REGULAR",
    ) -> bool:
        """
        Args:
            sizing: "MAX_VOLUME" (1.5배) | "REGULAR_VOLUME" (1.0배) | "MIN_VOLUME" (0.5배)
            exit_strategy: "REGULAR" | "TRAILING_EXIT"
        """
        if not self._is_trading_time():
            logger.debug(f"[매수거부] {stock_code} - 장외 시간 외")
            return False
        if stock_code in self.buying:
            return False
        if stock_code in self.positions:
            logger.debug(f"[매수거부] {stock_code} - 이미 보유")
            return False
        if len(self.positions) >= MAX_POSITIONS:
            logger.info(f"[매수거부] {stock_code} - 최대 보유 {MAX_POSITIONS}종목 초과")
            return False

        last = self.last_buy_ts.get(stock_code, 0)
        if time.time() - last < BUY_COOLDOWN_SEC:
            return False

        # 비중에 따른 동적 투자금액 산정
        multiplier = 1.0
        if sizing == "MAX_VOLUME":
            multiplier = 1.5
        elif sizing == "MIN_VOLUME":
            multiplier = 0.5
        required_amount = int(BUY_AMOUNT_PER_STOCK * multiplier)

        orderable = self.rest.get_orderable_amount()
        if orderable < required_amount:
            logger.warning(
                f"[매수거부] {stock_code} - 주문가능 금액 부족 "
                f"(필요 {required_amount:,} / 가능 {orderable:,})"
            )
            return False

        self.buying.add(stock_code)
        try:
            return self._execute_buy_legacy(stock_code, sizing, exit_strategy)
        finally:
            self.buying.discard(stock_code)

    def _execute_buy_legacy(
        self,
        stock_code: str,
        sizing: str = "REGULAR_VOLUME",
        exit_strategy: str = "REGULAR",
    ) -> bool:
        cur_price = self.rest.get_current_price(stock_code)
        if cur_price <= 0:
            logger.warning(f"[매수실패] {stock_code} - 현재가 조회 실패")
            return False

        order_price = round_to_tick(add_ticks(cur_price, BUY_PRICE_OFFSET_TICKS))

        multiplier = 1.0
        if sizing == "MAX_VOLUME":
            multiplier = 1.5
        elif sizing == "MIN_VOLUME":
            multiplier = 0.5

        target_amount = int(BUY_AMOUNT_PER_STOCK * multiplier)
        qty = target_amount // order_price
        if qty <= 0:
            logger.warning(f"[매수실패] {stock_code} - 수량 0 (price={order_price})")
            return False

        name = self.get_stock_name(stock_code)
        result = self.rest.buy_market_order(
            stock_code, qty=qty, price=order_price, trde_tp="0"
        )

        if result.get("return_code") != 0:
            msg = (
                f"❌[매수실패] {name}({stock_code}) {qty}주 @ {order_price:,}원\n"
                f"사유: {result.get('return_msg')}"
            )
            logger.error(msg)
            send_telegram(msg, target="order")
            return False

        self.positions[stock_code] = {
            "qty": qty,
            "avg_price": order_price,
            "bought_at": time.time(),
            "name": name,
            "ord_no": result.get("ord_no", ""),
            "sizing": sizing,
            "exit_strategy": exit_strategy,
        }
        self.last_buy_ts[stock_code] = time.time()

        sizing_tag = (
            "🔥비중확대"
            if sizing == "MAX_VOLUME"
            else ("⚠️비중축소" if sizing == "MIN_VOLUME" else "정상비중")
        )
        msg = (
            f"🟢 [매수주문] {name} ({stock_code}) - {sizing_tag}\n"
            f"수량: {qty}주\n"
            f"주문가: {order_price:,}원(현재가 {cur_price:,}+{BUY_PRICE_OFFSET_TICKS}틱)\n"
            f"금액: {qty * order_price:,}원\n"
            f"ord_no: {result.get('ord_no')}"
        )
        logger.info(msg)
        send_telegram(msg, target="order")
        return True

    def add_position_on_bounce(self, stock_code: str) -> bool:
        """
        20MA 이탈 후 체결강도 터졌을 때 추가 매수(평단가 내리기)
        StrategyManager에서 호출하여 사용합니다.
        """
        if stock_code not in self.positions:
            return False

        last = self.last_buy_ts.get(stock_code, 0)
        if time.time() - last < 1.0:
            return False

        cur_price = self.rest.get_current_price(stock_code)
        if cur_price <= 0:
            return False

        add_amount = BUY_AMOUNT_PER_STOCK // 2
        qty = add_amount // cur_price
        if qty <= 0:
            return False

        name = self.get_stock_name(stock_code)
        result = self.rest.buy_market_order(
            stock_code, qty=qty, price=cur_price, trde_tp="0"
        )

        if result.get("return_code") != 0:
            msg = (
                f"❌ [20MA 반등 추가매수 실패] {name}({stock_code}) {qty}주 @ {cur_price:,}원\n"
                f"사유: {result.get('return_msg')}"
            )
            logger.error(msg)
            send_telegram(msg, target="order")
            return False

        old_pos = self.positions[stock_code]
        old_qty = old_pos["qty"]
        old_avg = old_pos["avg_price"]

        new_qty = old_qty + qty
        new_avg = int((old_avg * old_qty + cur_price * qty) / new_qty)

        self.positions[stock_code]["qty"] = new_qty
        self.positions[stock_code]["avg_price"] = new_avg
        self.positions[stock_code][
            "last_add_at"
        ] = time.time()  # 추가매수 타임스탬프 분리 관리
        self.last_buy_ts[stock_code] = time.time()

        msg = (
            f"🚀 [20MA 반등 추가매수] {name} ({stock_code})\n"
            f"추가수량: {qty}주 @ {cur_price:,}원\n"
            f"총보유: {new_qty}주 (새 평단가: {new_avg:,}원)"
        )
        logger.info(msg)
        send_telegram(msg, target="order")
        return True

    # ─────────────────────────────────────
    # 매도 (자체 청산 로직, 레거시 호환용)
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
            self._execute_sell(
                stock_code, reason=f"시간정리 {pnl_pct:.2f}% ({held_min:.0f}분)"
            )
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

        msg = (
            f"💰 [매도주문] {name} ({stock_code})\n"
            f"사유: {reason}\n"
            f"수량: {pos['qty']}주\n"
            f"주문가: {order_price:,}원\n"
            f"평균단가: {pos['avg_price']:,}원\n"
            f"예상손익: {pnl:+,}원 ({pnl_pct_actual:+.2f}%)"
        )
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
        send_telegram(
            f"🔔 장마감 강제청산 시작: {len(self.positions)}종목", target="order"
        )

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
            lines.append(
                f"  {name}({code}): {pos['qty']}주 @ {pos['avg_price']:,}원 "
                f"({held:.0f}분 보유)"
            )
        return "\n".join(lines)
