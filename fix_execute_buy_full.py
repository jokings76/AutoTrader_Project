with open('core/strategy_manager.py', 'r', encoding='utf-8') as f:
    content = f.read()

start_marker = "    def _execute_buy(self, stock_code, stock_name, phase, info, sub_strategy):"
end_marker = "    def _record_watch_list(self, stock_code, stock_name, phase, info):"

start_idx = content.find(start_marker)
end_idx = content.find(end_marker)

if start_idx == -1 or end_idx == -1:
    raise SystemExit(f"앵커를 찾지 못했습니다. start={start_idx}, end={end_idx}")

new_func = '''    def _execute_buy(self, stock_code, stock_name, phase, info, sub_strategy):
        current_price = info["current_price"]
        sc = info.get("score")
        if sc is not None:
            logger.info(
                "[%s] %s 매수평가 통과 | score=%.2f/%.2f | %s",
                stock_code,
                stock_name,
                sc,
                info.get("score_threshold", 0),
                info.get("score_breakdown", ""),
            )

        position_amount, opt_info = self._resolve_position_amount(
            stock_code, sub_strategy
        )
        quantity = int(position_amount // current_price)
        if quantity < 1:
            logger.warning("[%s] %s 수량 0 -> skip", stock_code, stock_name)
            return

        if opt_info:
            logger.info(
                "[%s] 동적 비중 %.2fx -> %s원",
                stock_code,
                opt_info.get("final_weight", 1.0),
                f"{position_amount:,}",
            )

        self.pending.add(stock_code)
        try:
            ma_val = info.get("ma5") or 0
            if sub_strategy == "1B":
                entry_reason = f"Phase 1B 체결강도 (현재가 {current_price:,})"
            elif sub_strategy == "3":
                trigger = info.get("trigger")
                if trigger == "A":
                    entry_reason = (
                        f"Phase 3-A 강도 80-90-110 순차 (현재가 {current_price:,})"
                    )
                elif trigger == "B":
                    entry_reason = (
                        f"Phase 3-B 강도유지+매도잔량+누적매수 "
                        f"(시가 {info.get('opening_price', 0):,.0f} 기준, 현재가 {current_price:,})"
                    )
                else:
                    entry_reason = (
                        f"Phase 3 조건충족 1부유지 (현재가 {current_price:,})"
                    )
            elif sub_strategy == "1S":
                entry_reason = (
                    f"급등 진입 | 시초가+{info.get('surge_rate', 0)*100:.2f}% "
                    f"| vol x{info.get('volume_ratio', 0):.2f} (현재가 {current_price:,})"
                )
            elif sub_strategy == "1L":
                entry_reason = (
                    f"주도주 우선 진입 | 테마: {info.get('theme', '?')} "
                    f"| 체결강도 100이상 (현재가 {current_price:,})"
                )
            else:
                # 1A=5MA, 2=30MA (PHASE2_MA_PERIOD)
                ma_label = "5" if sub_strategy == "1A" else str(PHASE2_MA_PERIOD)
                entry_reason = (
                    f"Phase{sub_strategy} | "
                    f"MA{ma_label}={ma_val:,.0f} "
                    f"| vol x{info.get('volume_ratio', 0):.2f}"
                )
                if sub_strategy == "1A":
                    entry_reason += f" | 시초가+{info.get('surge_rate', 0)*100:.2f}%"
            if opt_info:
                entry_reason += f" | 비중x{opt_info.get('final_weight', 1.0):.2f}"

            if "vwap" in info:
                vwap = info["vwap"]
                gap_pct = ((current_price - vwap) / vwap * 100) if vwap > 0 else 0.0
                entry_reason += f" | VWAP {vwap:,.0f} (gap {gap_pct:+.2f}%, {info.get('vwap_score', 0)}점)"
                conf = info.get("vwap_confidence")
                if conf is not None:
                    entry_reason += f" | conf {conf:.2f}"
                gates = info.get("vwap_gates")
                if gates:
                    gate_str = ",".join(
                        f"{k}{'O' if v else 'X'}" for k, v in gates.items()
                    )
                    entry_reason += f" | gates[{gate_str}]"

            # 조건검색식 이름 프리픽스 (2026-07-06)
            cond_name = self._cond_names.get(stock_code, "알수없음")
            entry_reason = f"[{cond_name}] {entry_reason}"

            result = self.order_manager.buy(stock_code, quantity, price=0)
            if not result or not result.get("success"):
                err = (result or {}).get("error", "unknown")
                logger.error("[%s] 매수 실패: %s", stock_code, err)
                SystemEventRepository.log(
                    "ORDER_FAIL", f"BUY {stock_code}: {err}", "ERROR"
                )
                _notify(
                    f"매수 실패\\n{stock_code} {stock_name}\\n사유: {err}", target="order"
                )
                return

            trade_id = TradeRepository.insert_buy(
                stock_code=stock_code,
                stock_name=stock_name,
                buy_price=current_price,
                buy_quantity=quantity,
                strategy_phase=phase,
                sub_strategy=sub_strategy,
                entry_reason=entry_reason,
            )

            self.holdings[stock_code] = {
                "trade_id": trade_id,
                "buy_price": current_price,
                "buy_quantity": quantity,
                "buy_time": self._now(),
                "stock_name": stock_name,
                "strategy_phase": phase,
                "sub_strategy": sub_strategy,
                "highest_price": current_price,
                "ma20": None,
                "ma20_updated": None,
                "trigger": info.get("trigger"),
                "opening_price": info.get("opening_price", 0.0),
                "position_weight": (opt_info or {}).get("final_weight", 1.0),
                "warmup_until": self._now() + BUY_WARMUP,
                "entry_score": sc or 0.0,
            }

            self.sold_at.pop(stock_code, None)
            self._buy_success_count += 1

            logger.info(
                "BUY [%s] %s %d주 @ %s원 (%s) = %s원 | 워밍업 %ds",
                stock_code,
                stock_name,
                quantity,
                f"{current_price:,}",
                sub_strategy,
                f"{current_price * quantity:,}",
                int(BUY_WARMUP.total_seconds()),
            )
            SystemEventRepository.log(
                "BUY",
                f"{stock_code} {stock_name} {quantity}주 @ {current_price:,}원 [{sub_strategy}]",
                "INFO",
            )
            weight_str = f" (비중 {opt_info['final_weight']:.2f}x)" if opt_info else ""
            score_str = ""
            if sc is not None:
                score_str = (
                    f"\\n점수: {sc:.2f}/{info.get('score_threshold', 0):.2f} "
                    f"({info.get('score_breakdown', '')})"
                )
            _notify(
                f"매수 체결 [{sub_strategy}]\\n종목: {stock_name} ({stock_code})\\n"
                f"수량: {quantity}주 @ {current_price:,}원{weight_str}\\n"
                f"금액: {current_price * quantity:,}원\\n"
                f"전략: {entry_reason}{score_str}",
                target="order",
            )
            self._mark_watch_bought(stock_code)
        finally:
            self.pending.discard(stock_code)

    # ========================================
    # 워치리스트
    # ========================================
'''

new_content = content[:start_idx] + new_func + content[end_idx:]

with open('core/strategy_manager.py', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("교체 완료")