"""Repository 검증 테스트"""
import sys
import os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.connection import test_connection, close_pool, get_cursor
from db.repository import (
    TradeRepository,
    WatchListRepository,
    DailySummaryRepository,
    SystemEventRepository,
)

TEST_MARKER = "REPO_TEST_2026"


def t1_connection():
    print("\n[1] DB 연결")
    ok, msg = test_connection()
    assert ok, f"연결 실패: {msg}"
    print(f"  ✅ {msg[:70]}")


def t2_system_event():
    print("\n[2] system_events")
    eid = SystemEventRepository.log("TEST", TEST_MARKER, "INFO")
    assert eid is not None
    print(f"  ✅ insert id={eid}")

    rows = SystemEventRepository.find_recent(limit=3)
    assert len(rows) >= 1
    print(f"  ✅ 최근 {len(rows)}건 조회")


def t3_watch_list():
    print("\n[3] watch_list_log")
    wid = WatchListRepository.add(
        stock_code="005930",
        stock_name=TEST_MARKER,
        phase=1,
        open_price=70000,
        current_price=73500,
        surge_rate=5.0,
        volume_ratio=1.8,
        ma5=72500,
    )
    assert wid is not None
    print(f"  ✅ add id={wid}")

    WatchListRepository.mark_bought(wid)
    print(f"  ✅ mark_bought")

    todays = WatchListRepository.find_by_date(date.today())
    assert any(r["id"] == wid for r in todays)
    print(f"  ✅ 오늘 리스트 {len(todays)}건")


def t4_trade_lifecycle():
    print("\n[4] trades 라이프사이클 (매수→매도)")
    tid = TradeRepository.insert_buy(
        stock_code="005930",
        stock_name=TEST_MARKER,
        buy_price=73500,
        buy_quantity=27,
        strategy_phase=1,
        entry_reason="Phase1 5MA 터치 양봉",
    )
    print(f"  ✅ buy id={tid}")

    holdings = TradeRepository.find_holdings()
    assert any(h["id"] == tid for h in holdings)
    print(f"  ✅ holdings {len(holdings)}건")

    TradeRepository.update_sell(
        trade_id=tid,
        sell_price=75300,
        sell_quantity=27,
        exit_reason="익절 +2.5%",
        fee=500,
        tax=130,
    )
    closed = TradeRepository.find_by_id(tid)
    assert closed["status"] == "closed"
    print(f"  ✅ sell 완료 / 수익률 {float(closed['profit_rate']):.2f}% / 수익 {float(closed['profit_amount']):,.0f}원")


def t5_daily_summary():
    print("\n[5] daily_summary")
    today = date.today()
    sid = DailySummaryRepository.upsert(today, {
        "total_trades": 5,
        "winning_trades": 3,
        "losing_trades": 2,
        "win_rate": 60.0,
        "net_profit": 50000,
    })
    assert sid is not None
    print(f"  ✅ upsert (insert) id={sid}")

    DailySummaryRepository.upsert(today, {
        "total_trades": 6,
        "win_rate": 66.6,
    })
    print(f"  ✅ upsert (update)")

    s = DailySummaryRepository.find_by_date(today)
    assert s["total_trades"] == 6
    print(f"  ✅ 조회: total_trades={s['total_trades']}")


def cleanup():
    """TEST_MARKER 들어간 row만 정리"""
    print("\n[6] 테스트 데이터 정리")
    with get_cursor() as cur:
        cur.execute("DELETE FROM trades WHERE stock_name = %s", (TEST_MARKER,))
        n1 = cur.rowcount
        cur.execute("DELETE FROM watch_list_log WHERE stock_name = %s", (TEST_MARKER,))
        n2 = cur.rowcount
        cur.execute("DELETE FROM system_events WHERE event_message = %s", (TEST_MARKER,))
        n3 = cur.rowcount
        cur.execute(
            "DELETE FROM daily_summary WHERE trade_date = %s AND total_trades = 6",
            (date.today(),),
        )
        n4 = cur.rowcount
    print(f"  ✅ trades={n1}, watch={n2}, events={n3}, summary={n4}")


if __name__ == "__main__":
    print("=" * 60)
    print("Repository 검증 시작")
    print("=" * 60)
    try:
        t1_connection()
        t2_system_event()
        t3_watch_list()
        t4_trade_lifecycle()
        t5_daily_summary()
        cleanup()
        print("\n" + "=" * 60)
        print("✅ 전체 통과")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n❌ 검증 실패: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 예외: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    finally:
        close_pool()