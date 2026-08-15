# -*- coding: utf-8 -*-
"""2026-08-13 적용분 전용 스위트 — 매수 주가 상한(MAX_ENTRY_PRICE).

  [1] 상수·경계 — 10만원 '이상'이면 차단, 미만이면 통과
  [2] 두 진입 경로(계획 생성 / 주문 직전)가 모두 막히는가
  [3] 🔴 전일종가를 몰라도 상한이 살아 있는가 (결합 회귀 방지)
  [4] 진단 분류가 등락률과 분리돼 있는가
  [5] 기존 기능 무영향 — 물타기 bypass / 매도 경로 / 보유분
  [6] 롤백 (MAX_ENTRY_PRICE = 0)

실행: PYTHONIOENCODING=utf-8 AUTOTRADER_TEST_LOG=1 python test_patch_20260814.py
"""
import os
import sys

os.environ.setdefault("AUTOTRADER_TEST_LOG", "1")

import inspect                                          # noqa: E402
import time as _t                                       # noqa: E402
from datetime import datetime, timedelta                # noqa: E402

import core.strategy_manager as SM                      # noqa: E402
from core.phase1b_controller import Phase1BController   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


def section(t):
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


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
    def get_orderable_amount(self): return 50_000_000
    def get_current_price(self, code): return 0
    def get_minute_candles(self, code, interval=1, count=1, base_date=None): return []
    def get_daily_candles(self, code, base_dt=None, count=60): return []
    def get_basic_quote(self, code): return None
    def get_stock_change_rate(self, code): return 3.0


class _OrderMgr:
    def __init__(self): self.orders = []

    def buy(self, code, qty, price=0, sizing="R", exit_strategy="R",
            order_style="limit", ref_price=0):
        self.orders.append({"side": "buy", "code": code, "qty": qty})
        return {"success": True, "ord_no": "1", "price": ref_price or price or 9_700}

    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"side": "sell", "code": code, "qty": qty})
        return {"success": True, "ord_no": "2", "price": price or 9_700}

    def get_stock_name(self, code): return code


NOW = datetime(2026, 8, 14, 10, 0, 0)


def build():
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows, _Repo.sells, _Repo.updates = [], [], []
    return SM.StrategyManager(
        kiwoom_rest=_Rest(), order_manager=_OrderMgr(),
        phase1b_controller=Phase1BController(), portfolio_optimizer=None,
        now_func=lambda: NOW)


def prep(s, code, px):
    """진입 게이트 중 '가격 상한 외'는 전부 통과하도록 세팅."""
    s._cond_names[code] = "주도주상위"
    s._prev_closes[code] = px * 0.95        # 등락률 +5.3% (상·하한 통과)
    s._opening_prices[code] = px * 0.98
    s._first_seen[code] = _t.time() - 120   # 숙성 통과 + 유효창 안
    return s


def buys_of(s): return [o for o in s.order_manager.orders if o["side"] == "buy"]


# ══════════════════════════════════════════════════════════
section("[1] 상수 · 경계 — 10만원 '이상'이면 차단")
# ══════════════════════════════════════════════════════════
check("MAX_ENTRY_PRICE == 100,000", SM.MAX_ENTRY_PRICE == 100_000,
      f"{getattr(SM, 'MAX_ENTRY_PRICE', '(없음)'):,}")

for px, want_block in ((5_000, False), (50_000, False), (99_999, False),
                       (100_000, True), (139_700, True), (205_000, True)):
    s = build(); prep(s, "X", px)
    r = s._entry_change_reject("X", "1A", px)
    check(f"{px:>9,}원 -> {'차단' if want_block else '통과'}",
          (r is not None) is want_block, (r or "통과")[:40])

# 🔴 라벨에 수치를 박지 않았는가 — 상수를 바꾸면 거짓말이 된다
s = build(); prep(s, "X", 150_000)
_r = s._entry_change_reject("X", "1A", 150_000)
check("사유 문구가 상수를 그대로 읽는다(하드코딩 아님)",
      f"{SM.MAX_ENTRY_PRICE:,}" in (_r or ""), _r)


# ══════════════════════════════════════════════════════════
section("[2] 두 진입 경로가 모두 막히는가")
# ══════════════════════════════════════════════════════════
# 이 규칙의 유일한 지점은 _entry_change_reject 하나다(호출부를 늘리지 않았다).
# 🔴 '몇 번 등장하나'로 세면 안 된다 — 주석·사유문구까지 세어 거짓 실패가 난다.
#    **어느 메서드에 등장하나**로 센다.
_owners = sorted(
    n for n in dir(SM.StrategyManager)
    if callable(getattr(SM.StrategyManager, n, None)) and not n.startswith("__")
    and "MAX_ENTRY_PRICE" in (inspect.getsource(getattr(SM.StrategyManager, n))
                              if inspect.isfunction(getattr(SM.StrategyManager, n))
                              else "")
)
check("MAX_ENTRY_PRICE를 보는 메서드가 _entry_change_reject 하나뿐",
      _owners == ["_entry_change_reject"], str(_owners))

s = build(); prep(s, "H", 139_700)
s._open_entry_plan("H", "한국콜마", "1A",
                   {"current_price": 139_700, "entry_mode": "tick_driven"},
                   "1A", "주도주상위", 139_700)
check("계획 생성 경로에서 차단(슬롯을 묶지 않는다)", not s._entry_plans,
      f"plans={list(s._entry_plans)}")
check("  슬롯도 점유하지 않는다", s.occupied_slots() == 0, str(s.occupied_slots()))

s = build(); prep(s, "H", 139_700)
s._execute_buy("H", "한국콜마", "1A", {"current_price": 139_700}, sub_strategy="1A")
check("주문 직전 하드가드에서도 차단", not buys_of(s) and "H" not in s.holdings,
      f"매수 {len(buys_of(s))}건")

# 대조군 — 99,000원은 정상 매수돼야 한다(상한이 과잉 차단하지 않는다)
s = build(); prep(s, "L", 99_000)
s._execute_buy("L", "저가주", "1A", {"current_price": 99_000}, sub_strategy="1A")
check("🔴 [대조군] 99,000원은 정상 매수된다", bool(buys_of(s)) and "L" in s.holdings,
      f"매수 {len(buys_of(s))}건")


# ══════════════════════════════════════════════════════════
section("[3] 🔴 전일종가를 몰라도 상한이 살아 있는가")
# ══════════════════════════════════════════════════════════
# 상한 검사를 `if not prev_close: return None` **뒤**에 두면, 전일종가를 모르는
# 종목에서 상한이 조용히 같이 죽는다. 08-12에 _entry_delay_reject에서 정확히
# 같은 결합을 결함으로 잡았으므로 여기서 회귀를 못박는다.
s = build()
s._cond_names["N"] = "주도주상위"
s._first_seen["N"] = _t.time() - 120
s._get_prev_close = lambda c, p: None
check("전일종가 None이어도 139,700원은 차단된다",
      s._entry_change_reject("N", "1A", 139_700) is not None,
      str(s._entry_change_reject("N", "1A", 139_700)))
check("  전일종가 None + 저가주는 종전대로 통과('모름'이 매수를 막지 않는다)",
      s._entry_change_reject("N", "1A", 9_000) is None)

# 비정상 가격 입력에 예외를 던지지 않는다
for bad in (None, "", "abc", -1, 0):
    try:
        s._entry_change_reject("N", "1A", bad)
        ok = True
    except Exception as e:
        ok = False
    check(f"이상 입력 {bad!r}에 예외 없음", ok)


# ══════════════════════════════════════════════════════════
section("[4] 진단 분류 — 등락률과 뭉개지지 않는가")
# ══════════════════════════════════════════════════════════
cat = SM.StrategyManager._reject_category
_price = cat("주가 상한 초과 (139,700원 >= 100,000원)")
check("🔴 '기타'로 뭉개지지 않는다", _price != "기타", _price)
check("🔴 등락률 상한과 다른 분류다",
      _price != cat("등락률 상한 초과 (전일종가대비 +15.0% > +13%)"), _price)
check("🔴 '매수 컷오프'로 오분류되지 않는다(부분문자열 '상한' 함정)",
      "컷오프" not in _price, _price)
check("  등락률 분류는 그대로",
      cat("등락률 상한 초과 (전일종가대비 +15.0% > +13%)") == "등락률 상한 초과")


# ══════════════════════════════════════════════════════════
section("[5] 기존 기능 무영향")
# ══════════════════════════════════════════════════════════
check("물타기는 bypass_entry_gates=True라 상한을 받지 않는다",
      "bypass_entry_gates=True"
      in inspect.getsource(SM.StrategyManager._maybe_average_down))

# 🔴 매도는 이 게이트와 완전히 무관해야 한다 — 비싼 보유분이 못 팔면 재앙이다
s = build(); prep(s, "P", 139_700)
s.holdings["P"] = {
    "trade_id": 1, "stock_code": "P", "stock_name": "P", "buy_price": 139_700,
    "buy_quantity": 2, "qty": 2, "buy_time": NOW - timedelta(minutes=30),
    "warmup_until": NOW - timedelta(seconds=1), "sub_strategy": "1A",
    "strategy_phase": "1A", "origin_price": 139_700, "lowest_price": 139_700,
    "highest_price": 139_700, "stop_rate": None}
# ⚠️ 물타기(-3%)가 손절(-4.5%)보다 **먼저** 오므로, 손절만 보는 블록에서는
#    끄고 본다(08-11에 문서화된 규약 — 안 끄면 전부 '보유유지'로 실패한다).
_svad = SM.AVG_DOWN_ENABLED
SM.AVG_DOWN_ENABLED = False
try:
    s.on_price_update("P", int(139_700 * (1 + SM.STOP_LOSS_RATE)) - 1)
    check("🔴 10만원+ 보유분도 손절은 정상 발동(매도는 상한과 무관)",
          "P" not in s.holdings,
          f"보유 {s.holdings.get('P', {}).get('qty', '청산됨')}")
finally:
    SM.AVG_DOWN_ENABLED = _svad

# 🔴 알고 있어야 할 상호작용 — 물타기는 상한을 우회하므로 **상한 도입 전에 산**
#    10만원+ 보유분은 -3%에서 오히려 수량이 는다(2주 -> 4주). 상한이 켜진 뒤엔
#    신규 취득이 없어 곧 무의미해지지만, 전환기에는 실제로 일어난다.
s2 = build(); prep(s2, "R", 139_700)
s2.holdings["R"] = {
    "trade_id": 3, "stock_code": "R", "stock_name": "R", "buy_price": 139_700,
    "buy_quantity": 2, "qty": 2, "buy_time": NOW - timedelta(minutes=30),
    "warmup_until": NOW - timedelta(seconds=1), "sub_strategy": "1A",
    "strategy_phase": "1A", "origin_price": 139_700, "lowest_price": 139_700,
    "highest_price": 139_700, "stop_rate": None}
s2.on_price_update("R", int(139_700 * (1 - SM.AVG_DOWN_TRIGGER)) - 1)
_q = s2.holdings.get("R", {}).get("qty", 0)
check("⚠️ [기록] 물타기는 상한을 우회한다 — 기존 10만원+ 보유분은 수량이 는다",
      _q >= 4, f"보유 {_q}주 (2주 -> {_q}주)")

s = build(); prep(s, "Q", 139_700)
s.holdings["Q"] = dict(s.holdings.get("Q", {}), **{
    "trade_id": 2, "stock_code": "Q", "stock_name": "Q", "buy_price": 100_000,
    "buy_quantity": 4, "qty": 4, "buy_time": NOW - timedelta(minutes=30),
    "warmup_until": NOW - timedelta(seconds=1), "sub_strategy": "1A",
    "strategy_phase": "1A", "origin_price": 100_000, "lowest_price": 100_000,
    "highest_price": 100_000, "stop_rate": None})
s._prev_closes["Q"] = 95_000
s.on_price_update("Q", int(100_000 * (1 + SM.TAKE_PROFIT_CAP + 0.005)))
# 🆕 (2026-08-15) 확정익절 2%가 캡보다 먼저 잡아 **50% 분할**이 나간다.
#    이 검사의 요지는 "주가 상한이 **매도**까지 막지는 않는다"이므로,
#    완전 청산이 아니라 **매도가 실제로 나갔는가**로 본다.
_q_sold = any(o["side"] == "sell" for o in s.order_manager.orders)
check("🔴 10만원+ 보유분도 익절은 정상 발동(매수 상한이 매도를 막지 않는다)",
      _q_sold and ("Q" not in s.holdings or s.holdings["Q"]["qty"] < 4),
      f"보유 {s.holdings.get('Q', {}).get('qty', '청산됨')} / 매도 {_q_sold}")


# ══════════════════════════════════════════════════════════
section("[6] 롤백 — MAX_ENTRY_PRICE = 0")
# ══════════════════════════════════════════════════════════
_sv = SM.MAX_ENTRY_PRICE
SM.MAX_ENTRY_PRICE = 0
try:
    s = build(); prep(s, "X", 205_000)
    check("롤백(0): 상한이 사라진다",
          s._entry_change_reject("X", "1A", 205_000) is None)
    s = build(); prep(s, "X", 205_000)
    s._execute_buy("X", "고가주", "1A", {"current_price": 205_000}, sub_strategy="1A")
    check("롤백(0): 실제로 매수가 나간다", bool(buys_of(s)),
          f"매수 {len(buys_of(s))}건")
finally:
    SM.MAX_ENTRY_PRICE = _sv
check("롤백 후 원복", SM.MAX_ENTRY_PRICE == 100_000)


# ══════════════════════════════════════════════════════════
section("[7] 본전스톱 바닥 0.2% -> 1.0% (2026-08-13) — 🔄 롤백 경로")
# ══════════════════════════════════════════════════════════
# 🔴 (2026-08-15) 확정익절(FLAT_TP 2%)이 본전스톱을 **대체**했다. 이 절과
#    아래 [8]은 이제 `FLAT_TP_ENABLED = False` 롤백 경로를 검증한다 —
#    끈 기능의 배선 테스트를 지우면 되살릴 때 검증이 없다(08-10 교훈).
_SV_FLAT_814 = SM.FLAT_TP_ENABLED
SM.FLAT_TP_ENABLED = False

check("BREAKEVEN_FLOOR == 0.010", SM.BREAKEVEN_FLOOR == 0.010, str(SM.BREAKEVEN_FLOOR))
check("무장 문턱은 2.5% 그대로 (낮추면 승자를 자른다 — 격자 4/4열 악화)",
      SM.BREAKEVEN_TRIGGER == 0.025, str(SM.BREAKEVEN_TRIGGER))
check("🔴 불변식: 바닥 < 무장 (같거나 크면 무장 즉시 청산)",
      SM.BREAKEVEN_FLOOR < SM.BREAKEVEN_TRIGGER,
      f"{SM.BREAKEVEN_FLOOR} < {SM.BREAKEVEN_TRIGGER}")
check("🔴 불변식: 무장 문턱 < 익절캡 (무장 자체가 가능하다)",
      SM.BREAKEVEN_TRIGGER < SM.TAKE_PROFIT_CAP,
      f"{SM.BREAKEVEN_TRIGGER} < {SM.TAKE_PROFIT_CAP}")


def be_run(peak_net, back_to_net, floor=None):
    """순 peak_net까지 올렸다가 back_to_net으로 되돌린다. 청산되면 True."""
    sv = SM.BREAKEVEN_FLOOR
    if floor is not None:
        SM.BREAKEVEN_FLOOR = floor
    try:
        st = build()
        buy = 10_000
        st._prev_closes["B"] = buy * 0.95
        st._opening_prices["B"] = buy
        st._cond_names["B"] = "주도주상위"
        st.holdings["B"] = {
            "trade_id": 1, "stock_code": "B", "stock_name": "B",
            "buy_price": buy, "buy_quantity": 100, "qty": 100,
            "buy_time": NOW - timedelta(minutes=30),
            "warmup_until": NOW - timedelta(seconds=1), "sub_strategy": "1A",
            "strategy_phase": "1A", "origin_price": buy,
            "lowest_price": buy, "highest_price": buy, "stop_rate": -0.045}
        # 순수익률 -> 가격 역산 (수수료 0.23%)
        def px(net): return buy * (1 + net + 0.0023)
        st.on_price_update("B", px(peak_net))
        armed = bool(st.holdings.get("B", {}).get("breakeven_armed"))
        if "B" in st.holdings:
            st.on_price_update("B", px(back_to_net))
        # 🔴 (2026-08-14) 본전스톱은 **50% 분할**이 됐다. '청산됐는가'는 이제
        #    완전 청산이 아니라 **바닥이 발동했는가**로 판정해야 한다.
        p = st.holdings.get("B")
        fired = (p is None) or bool(p.get("be_partial_done"))
        return armed, fired
    finally:
        SM.BREAKEVEN_FLOOR = sv


# 무장 자체는 종전과 동일해야 한다(문턱을 안 건드렸으므로)
a, _ = be_run(0.024, 0.024)
check("순 +2.4%로는 무장하지 않는다(문턱 불변)", not a)
a, _ = be_run(0.026, 0.026)
check("순 +2.6%면 무장한다(문턱 불변)", a)

# 🔴 핵심 — 바닥이 실제로 1.0%에서 잘리는가
_, sold = be_run(0.030, 0.011)
check("🔴 순 +1.1%로 되돌리면 **팔지 않는다**(바닥 위)", not sold)
_, sold = be_run(0.030, 0.010)
check("🔴 순 +1.0%면 청산한다(바닥 도달)", sold)
_, sold = be_run(0.030, 0.005)
check("🔴 순 +0.5%면 청산한다 — 예전엔 여기까지 흘렀다", sold)

# A/B — 롤백값(0.002)과 실제로 갈리는가 (공허한 검사 방지)
_, sold_new = be_run(0.030, 0.005, floor=0.010)
_, sold_old = be_run(0.030, 0.005, floor=0.002)
check("🔴 [A/B] 순 +0.5%에서 신·구 사양이 갈린다 (검사가 공허하지 않다)",
      sold_new and not sold_old,
      f"바닥1.0%={'청산' if sold_new else '보유'} / 바닥0.2%={'청산' if sold_old else '보유'}")

# 롤백
_, sold = be_run(0.030, 0.003, floor=0.002)
check("롤백(0.002): 순 +0.3%면 아직 안 판다(구 사양 복원)", not sold)
_, sold = be_run(0.030, 0.001, floor=0.002)
check("롤백(0.002): 순 +0.1%면 청산(구 사양 복원)", sold)

# 무장 전에는 바닥이 아무 일도 하지 않는다 (회귀 방지)
_, sold = be_run(0.005, 0.003)
check("무장 전(순 +0.5%)에는 바닥이 발동하지 않는다", not sold)

# ── 🆕 (2026-08-14) 본전스톱 50% 분할 ──────────────────────────────
check("BREAKEVEN_EXIT_PARTIAL is True", SM.BREAKEVEN_EXIT_PARTIAL is True)


def be_split(peak_net=0.030, back_net=0.005, ticks=1, partial=False):
    st = build()
    buy = 10_000
    st._prev_closes["B"] = buy * 0.95
    st._opening_prices["B"] = buy
    st._cond_names["B"] = "주도주상위"
    st.holdings["B"] = {
        "trade_id": 1, "stock_code": "B", "stock_name": "B",
        "buy_price": buy, "buy_quantity": 100, "qty": 50 if partial else 100,
        "buy_time": NOW - timedelta(minutes=30),
        "warmup_until": NOW - timedelta(seconds=1), "sub_strategy": "1A",
        "strategy_phase": "1A", "origin_price": buy, "lowest_price": buy,
        "highest_price": buy, "stop_rate": -0.045, "partial_exited": partial}
    def _px(n): return int(buy * (1 + n + 0.0023))
    st.on_price_update("B", _px(peak_net))
    for _ in range(ticks):
        if "B" in st.holdings:
            st.on_price_update("B", _px(back_net))
    return st


st_s = be_split(ticks=4)
_q = st_s.holdings.get("B", {}).get("qty", "청산")
check("🔴 바닥 도달 시 절반만 판다", _q == 50, str(_q))
check("🔴 연속 4틱에도 잔량이 살아남는다(다음 틱 전량청산 방지)",
      "B" in st_s.holdings and st_s.holdings["B"]["qty"] == 50, str(_q))
check("  be_partial_done 표식", st_s.holdings.get("B", {}).get("be_partial_done") is True)
check("  잔량 트레일 기준 고점이 세팅된다",
      float(st_s.holdings.get("B", {}).get("trail_peak") or 0) > 0)

# 잔량은 무방비가 아니다
st_t = be_split(ticks=1)
if "B" in st_t.holdings:
    _pk = float(st_t.holdings["B"]["trail_peak"])
    st_t.on_price_update("B", int(_pk * (1 - SM.PARTIAL_EXIT_TRAIL) - 1))
check("🔴 잔량은 트레일로 청산된다", "B" not in st_t.holdings,
      str(st_t.holdings.get("B", {}).get("qty", "청산")))

st_c = be_split(ticks=1)
if "B" in st_c.holdings:
    st_c.on_price_update("B", int(10_000 * (1 + SM.TAKE_PROFIT_CAP + 0.0023) + 1))
check("🔴 잔량이 익절캡까지 가면 캡으로 청산", "B" not in st_c.holdings)

# 의도 보존 — 다른 규칙이 이미 분할한 포지션은 종전대로 잔량 전량
st_o = be_split(ticks=1, partial=True)
check("의도 보존: 이미 분할된 포지션은 잔량 전량 청산", "B" not in st_o.holdings,
      str(st_o.holdings.get("B", {}).get("qty", "청산")))

# 롤백
_svp = SM.BREAKEVEN_EXIT_PARTIAL
SM.BREAKEVEN_EXIT_PARTIAL = False
try:
    st_r = be_split(ticks=3)
    check("롤백(False): 종전대로 전량 청산", "B" not in st_r.holdings,
          str(st_r.holdings.get("B", {}).get("qty", "청산")))
finally:
    SM.BREAKEVEN_EXIT_PARTIAL = _svp


# 손절은 그대로 (바닥 상향이 하방 보호를 건드리지 않는다)
st = build()
st._prev_closes["S"] = 9_500
st._opening_prices["S"] = 10_000
st._cond_names["S"] = "주도주상위"
st.holdings["S"] = {
    "trade_id": 1, "stock_code": "S", "stock_name": "S", "buy_price": 10_000,
    "buy_quantity": 100, "qty": 100, "buy_time": NOW - timedelta(minutes=30),
    "warmup_until": NOW - timedelta(seconds=1), "sub_strategy": "1A",
    "strategy_phase": "1A", "origin_price": 10_000, "lowest_price": 10_000,
    "highest_price": 10_000, "stop_rate": -0.045}
_svad2 = SM.AVG_DOWN_ENABLED
SM.AVG_DOWN_ENABLED = False          # 물타기가 손절을 가로챈다(08-11 규약)
try:
    st.on_price_update("S", int(10_000 * (1 + SM.STOP_LOSS_RATE)) - 1)
    check("손절은 바닥 상향과 무관하게 정상 발동", "S" not in st.holdings)
finally:
    SM.AVG_DOWN_ENABLED = _svad2

SM.FLAT_TP_ENABLED = _SV_FLAT_814     # 본전스톱 롤백 컨텍스트 종료


print("\n" + "=" * 68)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
if FAIL:
    for f in FAIL:
        print("   FAIL:", f)
print("=" * 68)
sys.exit(1 if FAIL else 0)
