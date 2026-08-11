# -*- coding: utf-8 -*-
"""2026-08-12 변경 전용 스위트.

  [1] 물타기 11:00 컷오프 (사용자 지정)
  [2] 물타기 주문 거부 시 재시도 상한 — 🔴 결함 수정
      (상한이 없어서 50틱에 주문을 50회 냈다. 예수금 부족처럼 지속되는
       거부에서 REST가 429로 막히면 **매도 주문까지 밀린다**.)
  [3] 15:10 전량 강제청산 폐지 + 자동종료가 막히지 않는가
  [4] 지수가드 14:50 강제청산 폐지 / 본전청산 1단계는 생존
  [5] 오버나이트 보유분 manual 격리 / 당일분은 정상 복원(장중 재시작 보호)
  [6] 불변식 — 시간 기반 청산이 전부 닫혔는가

실행: PYTHONIOENCODING=utf-8 AUTOTRADER_TEST_LOG=1 python test_patch_20260812.py
"""
import os
import sys

os.environ.setdefault("AUTOTRADER_TEST_LOG", "1")

import inspect                                          # noqa: E402
from datetime import datetime, timedelta                # noqa: E402

import core.strategy_manager as SM                      # noqa: E402
import core.order_manager as OM                         # noqa: E402
from core.phase1b_controller import Phase1BController   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


def section(t):
    print("\n" + "=" * 66)
    print(t)
    print("=" * 66)


class _Repo:
    rows, sells, updates, holdings_src = [], [], [], []

    @classmethod
    def find_holdings(cls): return list(cls.holdings_src)

    @classmethod
    def find_by_date(cls, d): return []

    @classmethod
    def insert_buy(cls, **kw): cls.rows.append(kw); return len(cls.rows)

    @classmethod
    def update_sell(cls, trade_id=None, *a, **kw):
        cls.sells.append({"trade_id": trade_id, **kw}); return True

    @classmethod
    def update(cls, rid, data): cls.updates.append({"id": rid, **data}); return True

    @classmethod
    def add(cls, **kw): cls.rows.append(kw); return len(cls.rows)

    @classmethod
    def mark_bought(cls, i): return True

    @classmethod
    def log(cls, *a, **kw): return True

    @classmethod
    def find_closed_by_substrategy(cls, s): return []


class _Theme:
    def __init__(self, *a, **kw): self.code_to_theme = {}; self.leading_themes = []
    def fetch_themes_from_github(self): pass
    def start_auto_update(self, *a, **kw): pass
    def is_leading_theme_stock(self, code): return False


class _Rest:
    host = "https://mock"
    def get_index_change_rate(self, s="001"): return 0.0
    def get_orderable_amount(self): return 20_000_000
    def get_current_price(self, code): return 0
    def get_minute_candles(self, code, interval=1, count=1, base_date=None): return []
    def get_daily_candles(self, code, base_dt=None, count=60): return []
    def get_basic_quote(self, code): return None
    def get_stock_change_rate(self, code): return 3.0


class _OrderMgr:
    def __init__(self, buy_ok=True):
        self.orders = []
        self.buy_ok = buy_ok

    def buy(self, code, qty, price=0, sizing="R", exit_strategy="R",
            order_style="limit", ref_price=0):
        self.orders.append({"side": "buy", "code": code, "qty": qty})
        if not self.buy_ok:
            return {"success": False, "msg": "예수금 부족"}
        return {"success": True, "ord_no": "1", "price": ref_price or price or 9_700,
                "style": order_style}

    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"side": "sell", "code": code, "qty": qty})
        return {"success": True, "ord_no": "2", "price": price or 9_700,
                "style": order_style}

    def get_stock_name(self, code): return code


NOW = datetime(2026, 8, 12, 10, 0, 0)


def build(now_dt=NOW, buy_ok=True):
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows, _Repo.sells, _Repo.updates = [], [], []
    return SM.StrategyManager(
        kiwoom_rest=_Rest(), order_manager=_OrderMgr(buy_ok),
        phase1b_controller=Phase1BController(), portfolio_optimizer=None,
        now_func=lambda: now_dt)


def put(s, code="X", buy=10_000, qty=90, now_dt=None):
    now_dt = now_dt or s._now()
    s.holdings[code] = {
        "stock_code": code, "stock_name": code, "buy_price": buy,
        "buy_quantity": qty, "qty": qty,
        "buy_time": now_dt - timedelta(minutes=30),
        "warmup_until": now_dt - timedelta(seconds=1),
        "sub_strategy": "1A", "strategy_phase": "1A", "trade_id": 1,
        "origin_price": buy, "lowest_price": buy, "highest_price": buy,
        "stop_rate": None,
    }
    s._prev_closes[code] = buy * 0.9
    s._opening_prices[code] = buy
    s._cond_names[code] = "주도주상위"
    return s.holdings[code]


def buys(s): return [o for o in s.order_manager.orders if o["side"] == "buy"]
def sells(s): return [o for o in s.order_manager.orders if o["side"] == "sell"]


# ══════════════════════════════════════════════════════════
section("[1] 물타기 11:00 컷오프 (사용자 지정)")
# ══════════════════════════════════════════════════════════
check("AVG_DOWN_CUTOFF == 11:00",
      SM.AVG_DOWN_CUTOFF == SM.time(11, 0), SM.AVG_DOWN_CUTOFF.strftime("%H:%M"))
check("컷오프 < 진입종료(14:50)", SM.AVG_DOWN_CUTOFF < SM.PHASE1A_END)

for hh, mm, want in ((9, 0, True), (9, 30, True), (10, 59, True),
                     (11, 0, False), (11, 1, False),
                     (14, 55, False), (15, 9, False)):
    nd = datetime(2026, 8, 12, hh, mm, 0)
    s = build(now_dt=nd)
    p = put(s, now_dt=nd)
    s.on_price_update("X", 9_700)
    fired = bool(p.get("avg_down_done")) and len(buys(s)) == 1
    check(f"{hh:02d}:{mm:02d} -> {'물타기' if want else '차단'}", fired is want,
          f"매수 {len(buys(s))}건")

# 🔴 08-12 이전에는 15:09:59에도 발동했다 — 회복 시간이 원리적으로 없는
#    구간이라 왕복 수수료만 확정됐다. 그 회귀를 막는 단언이다.
s = build(now_dt=datetime(2026, 8, 12, 15, 9, 0))
p = put(s, now_dt=datetime(2026, 8, 12, 15, 9, 0))
s.on_price_update("X", 9_700)
check("🔴 회귀방지: 15:09에 물타기가 발동하지 않는다", len(buys(s)) == 0)

# 롤백 경로 — 컷오프를 늦추면 예전처럼 돈다
_sv = SM.AVG_DOWN_CUTOFF
SM.AVG_DOWN_CUTOFF = SM.time(15, 10)
try:
    s = build(now_dt=datetime(2026, 8, 12, 14, 0, 0))
    p = put(s, now_dt=datetime(2026, 8, 12, 14, 0, 0))
    s.on_price_update("X", 9_700)
    check("롤백: 컷오프를 15:10으로 두면 14:00에도 발동", len(buys(s)) == 1)
finally:
    SM.AVG_DOWN_CUTOFF = _sv


# ══════════════════════════════════════════════════════════
section("[2] 🔴 물타기 주문 거부 — 재시도 상한 (결함 수정)")
# ══════════════════════════════════════════════════════════
check("AVG_DOWN_MAX_RETRY == 3", SM.AVG_DOWN_MAX_RETRY == 3, str(SM.AVG_DOWN_MAX_RETRY))
check("재시도 쿨다운 5초", abs(SM.AVG_DOWN_RETRY_COOLDOWN_SEC - 5.0) < 1e-9)

# 쿨다운을 넘겨가며 50틱 — 상한에서 멈춰야 한다
s = build(buy_ok=False)
p = put(s)
base = datetime(2026, 8, 12, 10, 0, 0)
for i in range(50):
    s._now = lambda i=i: base + timedelta(seconds=i * 10)
    s.on_price_update("X", 9_700 - i)
check("🔴 50틱을 흘려도 주문 시도는 상한(3회)에서 멈춘다",
      len(buys(s)) <= SM.AVG_DOWN_MAX_RETRY,
      f"{len(buys(s))}회 (수정 전 50회 — REST 폭주)")
check("  포기하면 avg_down_done이 찍혀 재판정하지 않는다",
      bool(p.get("avg_down_done")))

# 같은 순간의 연타는 쿨다운이 막는다
s = build(buy_ok=False)
put(s)
for _ in range(50):
    s.on_price_update("X", 9_700)
check("쿨다운이 같은 순간의 연타를 막는다", len(buys(s)) <= 1, f"{len(buys(s))}회")

# 🔴 대조군 — 정상 주문은 종전대로 1회 체결되어야 한다
s = build(buy_ok=True)
p = put(s)
s.on_price_update("X", 9_700)
check("🔴 대조군: 정상 주문은 그대로 1회 체결", len(buys(s)) == 1)
check("  평단 9,850 / 수량 180주",
      abs(p["buy_price"] - 9_850) < 1 and p["qty"] == 180,
      f"{p['buy_price']:,.0f} / {p['qty']}주")

# 포기해도 손절은 정상 작동해야 한다(안전성의 핵심)
s = build(buy_ok=False)
p = put(s)
for i in range(10):
    s._now = lambda i=i: base + timedelta(seconds=i * 10)
    s.on_price_update("X", 9_700)
s._now = lambda: base + timedelta(seconds=200)
s.on_price_update("X", int(10_000 * (1 + SM.STOP_LOSS_RATE)) - 1)
check("🔴 물타기를 포기해도 손절은 정상 발동", len(sells(s)) == 1,
      f"매도 {len(sells(s))}건")


# ══════════════════════════════════════════════════════════
section("[3] 15:10 전량 강제청산 폐지 (사용자 지정)")
# ══════════════════════════════════════════════════════════
check("FORCE_CLOSE_ENABLED is False", OM.FORCE_CLOSE_ENABLED is False)
check("FORCE_CLOSE_TIME 상수는 보존(롤백용)", OM.FORCE_CLOSE_TIME == "15:10")

import main as M                                        # noqa: E402
_src = inspect.getsource(M.TradingBot.task_force_close_watcher)
check("워처가 FORCE_CLOSE_ENABLED를 본다", "FORCE_CLOSE_ENABLED" in _src)
check("🔴 끌 때 _force_close_done을 즉시 True로 세운다 (자동종료가 막히지 않게)",
      "_force_close_done = True" in _src.split("if not FORCE_CLOSE_ENABLED")[1][:400])
check("  그리고 곧바로 return 한다(청산 루프에 진입하지 않는다)",
      "return" in _src.split("if not FORCE_CLOSE_ENABLED")[1][:600])

# 장 마감 후에도 시간만으로는 안 판다
nd = datetime(2026, 8, 12, 15, 30, 0)
s = build(now_dt=nd)
put(s, now_dt=nd)
s.on_price_update("X", 10_000)
check("15:30 본전이면 청산 안 함 -> 오버나이트", len(sells(s)) == 0)
s = build(now_dt=nd)
put(s, now_dt=nd)
s.check_timeouts()
check("  폴링 경로(check_timeouts)로도 안 판다", len(sells(s)) == 0)

# 🔴 가격 기반 청산은 전부 살아 있어야 한다
for px, label in ((int(10_000 * (1 + SM.STOP_LOSS_RATE)) - 1, "손절"),
                  (int(10_000 * (1 + SM.TAKE_PROFIT_CAP + SM.ROUND_TRIP_COST)) + 2,
                   "익절캡")):
    s = build(now_dt=nd)
    put(s, now_dt=nd)
    s.on_price_update("X", px)
    check(f"🔴 15:30에도 {label}은 정상 작동", len(sells(s)) == 1,
          f"매도 {len(sells(s))}건")


# ══════════════════════════════════════════════════════════
section("[4] 지수가드 14:50 강제청산 폐지 / 1단계는 생존")
# ══════════════════════════════════════════════════════════
check("INDEX_GUARD_FORCE_CLOSE_ENABLED is False",
      SM.INDEX_GUARD_FORCE_CLOSE_ENABLED is False)
check("INDEX_GUARD_FORCE_CLOSE 시각 상수는 보존", SM.INDEX_GUARD_FORCE_CLOSE == SM.time(14, 50))


def guard(hh, mm):
    st = build(now_dt=datetime(2026, 8, 12, hh, mm, 0))
    st._kospi_rate, st._kosdaq_rate = -5.5, -1.0
    st._market_rate_at = st._now()
    return st


s = guard(14, 55)
put(s, now_dt=s._now())
s.on_price_update("X", 9_900)          # 손실 -1% — 옛 사양이면 강제청산
check("가드 발동 중이어도 14:50 강제청산 안 함", len(sells(s)) == 0,
      f"매도 {len(sells(s))}건 / 가드={s._is_index_guard_active()}")

# 1단계(11:30까지 본전 이상 청산)는 익절 계열이라 그대로 산다
s = guard(11, 10)
put(s, now_dt=s._now())
s.on_price_update("X", 10_300)         # 순 +0.77%
check("🔴 1단계 본전청산은 그대로 생존(익절 계열)", len(sells(s)) == 1,
      f"매도 {len(sells(s))}건")

# 롤백 — 되살리면 동작해야 한다(끈 기능의 배선 보존)
_sv = SM.INDEX_GUARD_FORCE_CLOSE_ENABLED
SM.INDEX_GUARD_FORCE_CLOSE_ENABLED = True
try:
    s = guard(14, 55)
    put(s, now_dt=s._now())
    s.on_price_update("X", 9_900)
    check("롤백(True): 14:50 강제청산 발동", len(sells(s)) == 1)
finally:
    SM.INDEX_GUARD_FORCE_CLOSE_ENABLED = _sv


# ══════════════════════════════════════════════════════════
section("[5] 오버나이트 manual 격리 / 당일분은 정상 복원")
# ══════════════════════════════════════════════════════════
check("OVERNIGHT_RESTORE_AS_MANUAL is True", SM.OVERNIGHT_RESTORE_AS_MANUAL is True)

_Repo.holdings_src = [
    {"id": 11, "stock_code": "AAA", "stock_name": "전일보유", "buy_price": 10_000,
     "buy_quantity": 90, "buy_time": datetime(2026, 8, 11, 13, 0, 0),
     "strategy_phase": "1A", "sub_strategy": "1A"},
    {"id": 22, "stock_code": "BBB", "stock_name": "당일보유", "buy_price": 20_000,
     "buy_quantity": 45, "buy_time": datetime(2026, 8, 12, 9, 5, 0),
     "strategy_phase": "1A", "sub_strategy": "1A"},
]
try:
    s = build(now_dt=datetime(2026, 8, 12, 9, 30, 0))
finally:
    _Repo.holdings_src = []

check("전일 보유분은 복원되지 않는다", "AAA" not in s.holdings, str(list(s.holdings)))
check("🔴 당일 보유분은 정상 복원된다 (장중 재시작 보호)", "BBB" in s.holdings,
      str(list(s.holdings)))
_manual = [u for u in _Repo.updates if u.get("status") == "manual"]
check("전일분 DB status가 'manual'로 바뀐다 (재시작 부활 방지)",
      len(_manual) == 1 and _manual[0]["id"] == 11, str(_manual))
check("  당일분은 status를 건드리지 않는다",
      not any(u["id"] == 22 for u in _Repo.updates), str(_Repo.updates))
check("격리분은 슬롯을 쓰지 않는다 (당일 신규매매에 영향 없음)",
      s.count_holdings_by_strategy("1A") == 1,
      f"1A 점유 {s.count_holdings_by_strategy('1A')}")
check("격리분은 손절 대상이 아니다(holdings에 없으므로 판정 자체를 안 탄다)",
      "AAA" not in s.holdings)

# 롤백 — False면 08-11까지처럼 전일분도 이어받는다
_sv = SM.OVERNIGHT_RESTORE_AS_MANUAL
SM.OVERNIGHT_RESTORE_AS_MANUAL = False
_Repo.holdings_src = [
    {"id": 33, "stock_code": "CCC", "stock_name": "전일보유", "buy_price": 10_000,
     "buy_quantity": 90, "buy_time": datetime(2026, 8, 11, 13, 0, 0),
     "strategy_phase": "1A", "sub_strategy": "1A"},
]
try:
    s = build(now_dt=datetime(2026, 8, 12, 9, 30, 0))
    check("롤백(False): 전일분도 그대로 복원", "CCC" in s.holdings)
finally:
    SM.OVERNIGHT_RESTORE_AS_MANUAL = _sv
    _Repo.holdings_src = []


# ══════════════════════════════════════════════════════════
section("[6] 불변식 — 시간 기반 자동청산이 전부 닫혔는가")
# ══════════════════════════════════════════════════════════
check("15:10 전량 강제청산 OFF", OM.FORCE_CLOSE_ENABLED is False)
check("지수가드 14:50 강제청산 OFF", SM.INDEX_GUARD_FORCE_CLOSE_ENABLED is False)
check("정체·시간정리 OFF (08-10부터)", SM.STAGNANT_EXIT_ENABLED is False)
check("🔴 => 남은 청산은 전부 '가격 기반'이다",
      OM.FORCE_CLOSE_ENABLED is False
      and SM.INDEX_GUARD_FORCE_CLOSE_ENABLED is False
      and SM.STAGNANT_EXIT_ENABLED is False)
# 신규매수 하드컷오프는 **그대로 살아 있어야** 한다 — 청산과 별개다.
check("신규매수 하드컷오프(15:10)는 유지", SM.ENTRY_HARD_CUTOFF == SM.time(15, 10))
check("진입 종료(14:50)도 유지", SM.PHASE1A_END == SM.time(14, 50))

print("\n" + "=" * 66)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
for f in FAIL:
    print("   FAIL:", f)
print("=" * 66)
sys.exit(1 if FAIL else 0)
