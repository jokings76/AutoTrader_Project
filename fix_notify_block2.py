with open('core/strategy_manager.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

start_idx = None
end_idx = None

for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith('weight_str = f" (') and "final_weight" in stripped:
        start_idx = i
    if start_idx is not None and stripped == "self.pending.discard(stock_code)":
        end_idx = i
        break

if start_idx is None or end_idx is None:
    print("앵커를 찾지 못했습니다. start_idx=", start_idx, "end_idx=", end_idx)
else:
    new_block = (
        '            weight_str = f" (비중 {opt_info[\'final_weight\']:.2f}x)" if opt_info else ""\n'
        '            score_str = ""\n'
        '            if sc is not None:\n'
        '                score_str = (\n'
        '                    f"\\n점수: {sc:.2f}/{info.get(\'score_threshold\', 0):.2f} "\n'
        '                    f"({info.get(\'score_breakdown\', \'\')})"\n'
        '                )\n'
        '            _notify(\n'
        '                f"매수 체결 [{sub_strategy}]\\n종목: {stock_name} ({stock_code})\\n"\n'
        '                f"수량: {quantity}주 @ {current_price:,}원{weight_str}\\n"\n'
        '                f"금액: {current_price * quantity:,}원\\n"\n'
        '                f"전략: {entry_reason}{score_str}",\n'
        '                target="order",\n'
        '            )\n'
        '            self._mark_watch_bought(stock_code)\n'
        '        finally:\n'
        '            self.pending.discard(stock_code)\n'
    )
    lines[start_idx:end_idx+1] = [new_block]
    with open('core/strategy_manager.py', 'w', encoding='utf-8') as f:
        f.writelines(lines)
    print(f"교체 완료 (원래 {start_idx+1}~{end_idx+1}번 줄)")