"""미청산 포지션을 '사용자 수동관리'로 넘기거나, 수동매도 결과를 DB에 기록하는 도구.

배경 (2026-08-06)
─────────────────
장중 긴급정지(09:19) 시 보유 3종목이 DB에 status='holding'으로 남았다.
그대로 재기동하면 `_restore_from_db()`가 이 행들을 holdings로 복원하고,
손절선(-3%)을 이미 넘긴 종목은 **기동 직후 시장가로 팔린다**.
사용자가 직접 판단해서 팔고 싶을 때 이걸 막아야 한다.

왜 status='manual'인가
─────────────────────
  · `find_holdings()`는 status='holding'만 조회한다 -> 봇이 복원하지 않는다.
    즉 손절·익절·15:10 강제청산 어디에도 걸리지 않는다. (목적)
  · 'closed'로 만들면 안 된다 — 팔지도 않았는데 손익이 확정 기록되고,
    켈리 계산(`find_closed_by_substrategy`)과 성과 통계가 오염된다.
  · 서버 잔고엔 남아 있으므로 `_detect_orphan_positions`가 **텔레그램으로
    1회 알려준다**. 사용자가 직접 산 종목(우리기술/엑스게이트)과 정확히
    같은 취급이 된다 — 이미 검증된 경로라 새 코드가 필요 없다.

사용법
──────
  python manual_position_tool.py list
      현재 미청산/수동관리 행 조회

  python manual_position_tool.py exclude
      status='holding' -> 'manual' (봇 관리에서 제외)

  python manual_position_tool.py restore
      status='manual' -> 'holding' (되돌리기. 봇이 다시 관리한다)

  python manual_position_tool.py close 073240 8050
      수동매도 완료 기록. 종목코드와 **실제 체결가**를 넘기면
      status='closed' + 손익이 정상 계산돼 통계에 반영된다.

⚠️ 봇이 돌고 있는 중에는 쓰지 말 것 — 메모리의 holdings와 DB가 어긋난다.
"""
import sys

from db.repository import TradeRepository, SystemEventRepository
from db.connection import get_cursor

MANUAL = "manual"
HOLDING = "holding"


def _rows(status: str) -> list[dict]:
    with get_cursor() as cur:
        cur.execute(
            "SELECT id, stock_code, stock_name, buy_price, buy_quantity, "
            "sub_strategy, status, buy_time FROM trades "
            "WHERE status = %s ORDER BY buy_time",
            (status,),
        )
        return [dict(r) for r in cur.fetchall()]


def cmd_list() -> int:
    for st in (HOLDING, MANUAL):
        rows = _rows(st)
        print(f"\n[{st}] {len(rows)}건")
        for r in rows:
            amt = float(r["buy_price"]) * int(r["buy_quantity"])
            print(
                f"  id={r['id']:<5} {r['stock_code']} {r['stock_name']:<12} "
                f"{int(r['buy_price']):>8,}원 x {r['buy_quantity']:>4}주 "
                f"= {amt:>12,.0f}원  ({r['sub_strategy']}, {r['buy_time']:%m-%d %H:%M})"
            )
    return 0


def _switch(src: str, dst: str, label: str) -> int:
    rows = _rows(src)
    if not rows:
        print(f"{src} 상태인 행이 없습니다. 할 일 없음.")
        return 0

    print(f"{label} 대상 {len(rows)}건:")
    for r in rows:
        print(f"  id={r['id']} {r['stock_code']} {r['stock_name']} "
              f"{int(r['buy_price']):,}원 x {r['buy_quantity']}주")

    with get_cursor() as cur:
        cur.execute(
            "UPDATE trades SET status = %s WHERE status = %s", (dst, src),
        )
        changed = cur.rowcount

    try:
        SystemEventRepository.log(
            event_type="MANUAL_POSITION",
            event_message=f"{label}: {changed}건 status {src} -> {dst} "
                          f"({', '.join(r['stock_code'] for r in rows)})",
            severity="WARNING",
        )
    except Exception as e:  # 이벤트 로그 실패가 본 작업을 되돌리지는 않는다
        print(f"  (이벤트 로그 실패, 무시: {e})")

    print(f"\n완료: {changed}건 -> status='{dst}'")
    if dst == MANUAL:
        print("이제 봇은 이 종목들을 복원하지 않습니다 "
              "(손절/익절/15:10 강제청산 대상 아님).")
        print("서버 잔고에는 남아 있으므로 기동 후 '미관리 잔고 감지' "
              "텔레그램이 종목당 1회 옵니다 — 정상입니다.")
    return 0


def cmd_close(stock_code: str, sell_price: float) -> int:
    """사용자가 HTS에서 판 뒤 실제 체결가를 기록한다."""
    rows = [r for r in _rows(MANUAL) if r["stock_code"] == stock_code]
    if not rows:
        print(f"{stock_code}: status='manual'인 행이 없습니다.")
        return 1
    r = rows[0]
    qty = int(r["buy_quantity"])
    buy_price = float(r["buy_price"])

    TradeRepository.update_sell(
        r["id"],
        sell_price=sell_price,
        sell_quantity=qty,
        exit_reason="사용자 수동매도 (봇 정지 중 보유분, 실제 체결가 입력)",
    )
    rate = (sell_price - buy_price) / buy_price * 100 if buy_price else 0.0
    print(
        f"기록 완료: {r['stock_name']} ({stock_code}) "
        f"{buy_price:,.0f} -> {sell_price:,.0f} x {qty}주 = {rate:+.2f}% (수수료 전)"
    )
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        return cmd_list()
    if cmd == "exclude":
        return _switch(HOLDING, MANUAL, "봇 관리 제외")
    if cmd == "restore":
        return _switch(MANUAL, HOLDING, "봇 관리 복귀")
    if cmd == "close":
        if len(argv) < 4:
            print("사용법: python manual_position_tool.py close <종목코드> <실제체결가>")
            return 1
        return cmd_close(argv[2], float(argv[3]))
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
