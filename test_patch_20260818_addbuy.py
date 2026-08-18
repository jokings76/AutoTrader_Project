# -*- coding: utf-8 -*-
"""2026-08-18 검증분 — 🔴 추가매수 10:00 컷오프 (사용자 지정).

  "10시 이후에는 무조건 추가매수를 하지 않는다."

  추가매수 = **이미 보유한 종목에 물량을 더 얹는 두 경로**다.
    ① `_maybe_average_down`            -3% 물타기 (조건 없음)
    ② `_rescue_gate`/`_do_rescue_add`  손절 대신 추가매수 (조건 3개 AND)
  신규 진입(되돌림 1·2차 트랜치)은 여기 해당하지 않는다 — [6]에서 실증한다.

  [1] 상수 — 두 경로가 **같은 시각**을 쓰는가 (갈라지면 반쪽만 돈다)
  [2] 물타기 경계 — 컷오프 1분 전 발동 / 정각·이후 차단
  [3] rescue-add 경계 — 컷오프 전 관찰 시작 / 정각 이후 즉시 손절
  [4] 🔴 A/B 무효반증 — 컷오프를 11:00(구 사양)으로 되돌리면 10:30에 둘 다
      발동한다. 즉 이 변경은 **실제로 동작을 바꾼다**(공허하지 않다).
  [5] 관찰 중에 컷오프를 넘기면 추가매수 없이 손절로 수렴한다
  [6] 🔴 컷오프는 **신규 매수를 막지 않는다** — 10시 이후에도 새 종목은 산다
  [7] 컷오프는 **손절을 막지 않는다** (안전측 수렴)
  [8] 롤백 — 상수 한 줄로 08-12~08-17 사양 복귀

  ⚠️ 경계 시각은 SM.ADD_BUY_CUTOFF에서 **역산**한다. 08-18에 컷오프를 옮기면서
     하드코딩 10:00 픽스처를 쓰던 스위트 5개가 한꺼번에 실패했다
     (08-05 / 08-11 / 08-12 / 08-14 / 08-15 + replay). 같은 실수를 반복하지 말 것.

실행: PYTHONIOENCODING=utf-8 AUTOTRADER_TEST_LOG=1 python test_patch_20260818_addbuy.py
"""
import os
import sys

os.environ.setdefault("AUTOTRADER_TEST_LOG", "1")

import time as _pytime                                   # noqa: E402
from datetime import date as _date, datetime, timedelta  # noqa: E402

import core.strategy_manager as SM                       # noqa: E402
from core.phase1b_controller import Phase1BController    # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s | %s%s" % ("OK  " if cond else "FAIL", name,
                           (" -- " + detail) if detail else ""))


def section(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


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
    def __init__(self, *a, **kw):
        self.code_to_theme = {}
        self.leading_themes = []

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
    def __init__(self):
        self.orders = []

    def buy(self, code, qty, price=0, sizing="R", exit_strategy="R",
            order_style="limit", ref_price=0):
        self.orders.append({"side": "buy", "code": code, "qty": qty})
        return {"success": True, "ord_no": "1",
                "price": ref_price or price or 9_700, "style": order_style}

    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"side": "sell", "code": code, "qty": qty})
        return {"success": True, "ord_no": "2", "price": price or 9_700,
                "style": order_style}

    def get_stock_name(self, code): return code


DAY = _date(2026, 8, 18)
CUT = SM.ADD_BUY_CUTOFF


def at(hh, mm, ss=0):
    return datetime(DAY.year, DAY.month, DAY.day, hh, mm, ss)


def before_cut(minutes=1):
    return datetime.combine(DAY, CUT) - timedelta(minutes=minutes)


def after_cut(minutes=0):
    return datetime.combine(DAY, CUT) + timedelta(minutes=minutes)


def build(now_dt):
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows, _Repo.sells, _Repo.updates = [], [], []
    return SM.StrategyManager(
        kiwoom_rest=_Rest(), order_manager=_OrderMgr(),
        phase1b_controller=Phase1BController(), portfolio_optimizer=None,
        now_func=lambda: now_dt)


def put(s, code="X", buy=10_000, qty=90, now_dt=None):
    now_dt = now_dt or s._now()
    s.holdings[code] = {
        "stock_code": code, "stock_name": code, "buy_price": buy,
        "buy_quantity": qty, "qty": qty,
        "buy_time": now_dt - timedelta(minutes=20),
        "warmup_until": now_dt - timedelta(seconds=1),
        "sub_strategy": "1A", "strategy_phase": "1A", "trade_id": 1,
        "origin_price": buy, "lowest_price": buy, "highest_price": buy,
        "stop_rate": None,
    }
    s._prev_closes[code] = buy * 0.9
    s._opening_prices[code] = buy
    s._cond_names[code] = "주도주상위"
    return s.holdings[code]


def feed(s, code, base=100.0):
    """rescue 조건 3개(거래대금 가속·강도·반등)를 전부 성립시키는 틱 주입."""
    s.phase1b.start_watching(code)
    tf = s.phase1b.trade_flow
    now = _pytime.time()
    for i in range(40):
        tf.add_tick(code, 10_000, "buy", 1, now=now - 110 + i)
    for i in range(10):
        tf.add_tick(code, 9_700, "sell", 1, now=now - 28 + i)
    for i in range(10):
        tf.add_tick(code, 9_730, "buy", 400, now=now - 10 + i)
    pos = s.holdings.get(code)
    if pos is not None:
        pos["strength_baseline"] = base
    return tf


def buys(s):
    return [o for o in s.order_manager.orders if o["side"] == "buy"]


def sells(s):
    return [o for o in s.order_manager.orders if o["side"] == "sell"]


def reasons():
    return " ".join(str(x.get("exit_reason") or "") for x in _Repo.sells)


# ══════════════════════════════════════════════════════════════════
section("[1] 상수 — 두 경로가 같은 시각을 쓰는가")
# ══════════════════════════════════════════════════════════════════
check("ADD_BUY_CUTOFF == 10:00", CUT == SM.time(10, 0), CUT.strftime("%H:%M"))
check("🔴 물타기 컷오프 == ADD_BUY_CUTOFF", SM.AVG_DOWN_CUTOFF == CUT,
      SM.AVG_DOWN_CUTOFF.strftime("%H:%M"))
check("🔴 rescue-add 컷오프 == ADD_BUY_CUTOFF", SM.RESCUE_ADD_CUTOFF == CUT,
      SM.RESCUE_ADD_CUTOFF.strftime("%H:%M"))
check("두 경로가 갈라져 있지 않다(반쪽 동작 방지)",
      SM.AVG_DOWN_CUTOFF == SM.RESCUE_ADD_CUTOFF)
check("컷오프 < 진입 종료(14:50)", CUT < SM.PHASE1A_END)
check("컷오프 > 장 시작(09:00) — 오전 초반은 살아 있다", CUT > SM.GROUP_A_START)
check("추가매수 두 경로가 모두 켜져 있다(컷오프가 도달 가능한 상태)",
      SM.AVG_DOWN_ENABLED and SM.RESCUE_ADD_ENABLED)

# ══════════════════════════════════════════════════════════════════
section("[2] 물타기 경계 — 컷오프 정각부터 차단")
# ══════════════════════════════════════════════════════════════════
for nd, want, lbl in ((before_cut(30), True, "컷오프 30분 전"),
                      (before_cut(1), True, "컷오프 1분 전"),
                      (after_cut(0), False, "컷오프 정각"),
                      (after_cut(1), False, "컷오프 1분 후"),
                      (at(14, 30), False, "14:30")):
    s = build(nd)
    p = put(s, now_dt=nd)
    s.on_price_update("X", 9_700)          # 원가 -3.0%
    fired = len(buys(s)) == 1
    check("%s (%s) -> %s" % (lbl, nd.strftime("%H:%M"),
                             "물타기" if want else "차단"),
          fired is want, "매수 %d건" % len(buys(s)))

s = build(after_cut(0))
p = put(s, now_dt=after_cut(0))
s.on_price_update("X", 9_700)
check("차단 시 avg_down_done이 찍혀 매 틱 재판정하지 않는다",
      bool(p.get("avg_down_done")))

# ══════════════════════════════════════════════════════════════════
section("[3] rescue-add 경계 — 컷오프 정각부터 즉시 손절")
# ══════════════════════════════════════════════════════════════════
# 🔴 물타기가 손절선(-4.5%)보다 **먼저** 개입하므로 rescue를 보는 동안은 끈다.
_SV_AD = SM.AVG_DOWN_ENABLED
SM.AVG_DOWN_ENABLED = False
try:
    nd = before_cut(5)
    s = build(nd)
    p = put(s, now_dt=nd)
    feed(s, "X")
    s.on_price_update("X", 9_540)          # 손절선 첫 이탈
    check("컷오프 전: 매도하지 않고 관찰 시작",
          not sells(s) and p.get("rescue_watch_until") is not None,
          "매도 %d건" % len(sells(s)))

    nd = after_cut(0)
    s = build(nd)
    p = put(s, now_dt=nd)
    feed(s, "X")
    s.on_price_update("X", 9_540)
    check("🔴 컷오프 정각: 조건이 다 성립해도 관찰조차 시작하지 않는다",
          p.get("rescue_watch_until") is None)
    check("  대신 그냥 손절된다(안전측 수렴)", len(sells(s)) == 1,
          "매도 %d건" % len(sells(s)))
    check("  추가매수 주문은 0건", len(buys(s)) == 0, "매수 %d건" % len(buys(s)))
    check("  rescue_added가 찍히지 않는다", not p.get("rescue_added"))

    nd = at(14, 45)
    s = build(nd)
    p = put(s, now_dt=nd)
    feed(s, "X")
    s.on_price_update("X", 9_540)
    check("14:45에도 차단 (08-17까지는 시간 제한이 아예 없었다)",
          len(buys(s)) == 0 and len(sells(s)) == 1)

    # ══════════════════════════════════════════════════════════════
    section("[5] 관찰 중에 컷오프를 넘기면 추가매수 없이 손절")
    # ══════════════════════════════════════════════════════════════
    # ⚠️ 반등가는 **손절선(-4.5% = 9,550) 아래**를 유지해야 한다. 위로 올리면
    #    애초에 청산 분기에 들어오지 않아 "매도 0건"이 나오는데, 그건 컷오프
    #    때문이 아니라 가격 때문이다(원인이 섞인 거짓 통과/실패).
    nd = before_cut(1)
    s = build(nd)
    p = put(s, now_dt=nd)
    feed(s, "X")
    s.on_price_update("X", 9_540)
    check("관찰 시작됨(컷오프 1분 전)", p.get("rescue_watch_until") is not None)
    s.on_price_update("X", 9_500)          # 관찰 중 저점 갱신
    check("  관찰 저점이 갱신된다", p.get("rescue_low") == 9_500)
    s._now = lambda: after_cut(0)          # 관찰 중 컷오프 경과
    s.on_price_update("X", 9_535)          # 저점 대비 +0.37% = 반등 확증 성립
    check("🔴 반등이 확증돼도 추가매수하지 않는다", len(buys(s)) == 0,
          "매수 %d건" % len(buys(s)))
    check("  손절로 수렴한다", len(sells(s)) == 1, "매도 %d건" % len(sells(s)))

    # ══════════════════════════════════════════════════════════════
    section("[4] 🔴 A/B 무효반증 — 구 사양으로 되돌리면 10:30에 발동하는가")
    # ══════════════════════════════════════════════════════════════
    # 이 블록이 통과하지 않으면 [2][3]은 '원래부터 안 되던 것'을 본 것이고,
    # 이번 변경은 무효다. 반드시 **같은 입력**으로 값만 바꿔 비교한다.
    _sv_r = SM.RESCUE_ADD_CUTOFF
    nd = at(10, 30)
    s = build(nd)
    p = put(s, now_dt=nd)
    feed(s, "X")
    s.on_price_update("X", 9_540)
    check("A(현행 10:00): 10:30 rescue 차단",
          len(buys(s)) == 0 and len(sells(s)) == 1)
    SM.RESCUE_ADD_CUTOFF = SM.time(11, 0)
    try:
        s = build(nd)
        p = put(s, now_dt=nd)
        feed(s, "X")
        s.on_price_update("X", 9_540)
        check("🔴 B(구 11:00): 같은 입력에서 10:30 관찰이 시작된다 — A/B가 갈린다",
              p.get("rescue_watch_until") is not None and not sells(s),
              "매도 %d건" % len(sells(s)))
    finally:
        SM.RESCUE_ADD_CUTOFF = _sv_r
finally:
    SM.AVG_DOWN_ENABLED = _SV_AD

_sv_a = SM.AVG_DOWN_CUTOFF
nd = at(10, 30)
s = build(nd)
put(s, now_dt=nd)
s.on_price_update("X", 9_700)
check("A(현행 10:00): 10:30 물타기 차단", len(buys(s)) == 0)
SM.AVG_DOWN_CUTOFF = SM.time(11, 0)
try:
    s = build(nd)
    put(s, now_dt=nd)
    s.on_price_update("X", 9_700)
    check("🔴 B(구 11:00): 같은 입력에서 10:30 물타기가 발동 — A/B가 갈린다",
          len(buys(s)) == 1, "매수 %d건" % len(buys(s)))
finally:
    SM.AVG_DOWN_CUTOFF = _sv_a

# ══════════════════════════════════════════════════════════════════
section("[6] 🔴 컷오프는 신규 매수를 막지 않는다")
# ══════════════════════════════════════════════════════════════════
# 사용자 지정은 "추가매수 금지"지 "매수 금지"가 아니다. 되돌림 트랜치는
# 한 종목의 최초 1슬롯을 나눠 채우는 것이므로 컷오프 대상이 아니다.
nd = at(11, 0)
s = build(nd)
s._prev_closes["N1"] = 9_000
s._opening_prices["N1"] = 9_500
s._cond_names["N1"] = "주도주상위"
# `_execute_buy`는 반환값이 없다(None) — 체결 여부는 **주문과 보유**로 본다.
s._execute_buy("N1", "N1", "1A", {"current_price": 9_800}, sub_strategy="1A")
check("11:00에 신규 매수는 정상 체결된다",
      len(buys(s)) == 1 and "N1" in s.holdings,
      "매수 %d건 / 보유 %s" % (len(buys(s)), sorted(s.holdings)))
check("  신규 매수는 rescue/물타기 표식을 남기지 않는다",
      not s.holdings.get("N1", {}).get("rescue_added")
      and not s.holdings.get("N1", {}).get("avg_down_done"))
check("컷오프가 신규매수 하드컷오프(15:10)와 별개다",
      SM.ENTRY_HARD_CUTOFF != CUT,
      "%s vs %s" % (SM.ENTRY_HARD_CUTOFF, CUT))

# ══════════════════════════════════════════════════════════════════
section("[7] 컷오프는 손절·최종선을 막지 않는다")
# ══════════════════════════════════════════════════════════════════
nd = at(13, 0)
s = build(nd)
p = put(s, now_dt=nd)
s.on_price_update("X", 9_500)              # -5.0% : 손절선 아래
check("13:00 손절은 정상 발동", len(sells(s)) == 1, "매도 %d건" % len(sells(s)))

nd = at(13, 0)
s = build(nd)
p = put(s, now_dt=nd)
p["avg_down_done"] = True                  # 오전에 물탄 포지션
p["buy_price"] = 9_850
s.on_price_update("X", 9_400)              # 원가 -6.0%
check("오전에 물탄 포지션의 '구조 후 최종손절'도 정상 발동",
      len(sells(s)) == 1 and "최종손절" in reasons(), reasons()[:40])

# ══════════════════════════════════════════════════════════════════
section("[8] 롤백 — 상수 한 줄로 08-12~08-17 사양 복귀")
# ══════════════════════════════════════════════════════════════════
_sv_a, _sv_r = SM.AVG_DOWN_CUTOFF, SM.RESCUE_ADD_CUTOFF
SM.AVG_DOWN_CUTOFF = SM.time(11, 0)
SM.RESCUE_ADD_CUTOFF = SM.time(15, 10)
try:
    nd = at(10, 45)
    s = build(nd)
    put(s, now_dt=nd)
    s.on_price_update("X", 9_700)
    check("롤백: 물타기 11:00이면 10:45에 발동", len(buys(s)) == 1)

    _sv_ad = SM.AVG_DOWN_ENABLED
    SM.AVG_DOWN_ENABLED = False
    try:
        nd = at(14, 0)
        s = build(nd)
        p = put(s, now_dt=nd)
        feed(s, "X")
        s.on_price_update("X", 9_540)
        check("롤백: rescue 15:10이면 14:00에 관찰 시작(08-17까지의 동작)",
              p.get("rescue_watch_until") is not None)
    finally:
        SM.AVG_DOWN_ENABLED = _sv_ad
finally:
    SM.AVG_DOWN_CUTOFF, SM.RESCUE_ADD_CUTOFF = _sv_a, _sv_r

check("롤백 후 원복됨", SM.AVG_DOWN_CUTOFF == CUT and SM.RESCUE_ADD_CUTOFF == CUT)

print("\n" + "=" * 70)
print("통과 %d건 / 실패 %d건" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("   FAIL:", f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
