# -*- coding: utf-8 -*-
"""2026-08-18 검증분 ② — 🔴 추가매수 지수 문턱 -1% (사용자 지정).

  "지수 1% 이상 하락 시에는 무조건 물타기는 없는걸로."

  이 규칙은 08-18 오전에 넣은 **10:00 컷오프와 세트**다. 실측상 둘은 서로
  다른 건을 막는다(08-18 물타기 3건):
      일성건설  09:08  나쁜쪽 지수 -1.16%  <- 이 규칙이 막는다
      아진엑스텍 10:44  나쁜쪽 지수 -0.28%  <- 컷오프가 막는다
      피노      10:57  나쁜쪽 지수 -0.74%  <- 컷오프가 막는다

  🔴 **기존 INDEX_GUARD로는 대체 불가**다. 그쪽은 -5% 이하 **+ 11:00 이후**라야
     켜지는데 추가매수는 10:00에 끊긴다 — 두 창이 겹치지 않아
     `AVG_DOWN_BLOCK_ON_INDEX_GUARD`는 구조적으로 도달 불가가 됐다.
     [7]에서 이 사실 자체를 단언한다.

  [1] 상수 — 두 경로가 **같은 문턱**을 쓰는가 (갈라지면 반쪽만 돈다)
  [2] 물타기 경계 — -0.99% 발동 / -1.00% 차단 / -1.01% 차단
  [3] 코스피/코스닥 중 **나쁜 쪽**을 본다 (한쪽만 밀려도 차단)
  [4] 🔴 그날 종료 래치 — 차단 후 지수가 회복해도 그 종목은 다시 안 산다
      (사용자 선택. 순간판정만 하면 08-18에 09:08 차단 -> 09:10 매수로 무효가 된다)
  [5] rescue-add — 차단 시 관찰조차 시작하지 않고 손절로 수렴
  [6] 🔴 A/B 무효반증 — 문턱을 None으로 두면 **같은 입력**에서 물타기가 발동한다
  [7] 🔴 기존 INDEX_GUARD와 독립 — 그 가드가 꺼진 시각·수준에서도 이 규칙은 돈다
  [8] 시각 컷오프와 **OR** — 둘 중 하나만 걸려도 안 산다
  [9] 신규 매수는 막지 않는다 / 손절은 살아 있다
  [10] fail-open — 지수 조회 실패(0.0)면 차단하지 않는다(기존 관례)
  [11] 롤백 — 상수 한 줄로 08-18 오전 사양 복귀

  ⚠️ 경계는 SM.ADD_BUY_INDEX_DROP_PCT에서 **역산**한다. 문턱을 옮겨도 안 깨진다.

실행: PYTHONIOENCODING=utf-8 AUTOTRADER_TEST_LOG=1 python test_patch_20260818_index.py
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
    """지수를 테스트에서 갈아끼울 수 있게 한 것 외엔 표준 스텁과 동일."""
    host = "https://mock"

    def __init__(self, kospi=0.0, kosdaq=0.0):
        self.kospi, self.kosdaq = kospi, kosdaq
        self.index_calls = 0

    def get_index_change_rate(self, sector_code="001"):
        self.index_calls += 1
        return self.kospi if str(sector_code) == "001" else self.kosdaq

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
THR = SM.ADD_BUY_INDEX_DROP_PCT          # -1.0
CUT = SM.ADD_BUY_CUTOFF                  # 10:00

# 🔴 경계는 문턱에서 역산한다 — 값을 옮겨도 안 깨진다.
OK_RATE = THR + 0.01                     # -0.99% : 통과해야 한다
EDGE_RATE = THR                          # -1.00% : 차단(이하 = 차단)
BAD_RATE = THR - 0.01                    # -1.01% : 차단


def at(hh, mm, ss=0):
    return datetime(DAY.year, DAY.month, DAY.day, hh, mm, ss)


SAFE_T = datetime.combine(DAY, CUT) - timedelta(minutes=30)   # 09:30 — 컷오프 통과


def build(now_dt=None, kospi=0.0, kosdaq=0.0):
    now_dt = now_dt or SAFE_T
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows, _Repo.sells, _Repo.updates = [], [], []
    rest = _Rest(kospi=kospi, kosdaq=kosdaq)
    s = SM.StrategyManager(
        kiwoom_rest=rest, order_manager=_OrderMgr(),
        phase1b_controller=Phase1BController(), portfolio_optimizer=None,
        now_func=lambda: now_dt)
    s._test_rest = rest
    return s


def set_now(s, dt):
    """시계를 옮기고 지수 60초 캐시를 만료시킨다(안 하면 새 지수가 안 읽힌다)."""
    s._now = lambda: dt
    s._market_rate_at = None


def set_index(s, kospi=None, kosdaq=None):
    if kospi is not None:
        s._test_rest.kospi = kospi
    if kosdaq is not None:
        s._test_rest.kosdaq = kosdaq
    s._market_rate_at = None             # 캐시 만료


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


# ══════════════════════════════════════════════════════════════════
section("[1] 상수 — 두 경로가 같은 문턱을 쓰는가")
# ══════════════════════════════════════════════════════════════════
check("ADD_BUY_INDEX_DROP_PCT == -1.0", THR == -1.0, str(THR))
check("🔴 물타기 문턱 == ADD_BUY_INDEX_DROP_PCT",
      SM.AVG_DOWN_INDEX_DROP_PCT == THR, str(SM.AVG_DOWN_INDEX_DROP_PCT))
check("🔴 rescue-add 문턱 == ADD_BUY_INDEX_DROP_PCT",
      SM.RESCUE_ADD_INDEX_DROP_PCT == THR, str(SM.RESCUE_ADD_INDEX_DROP_PCT))
check("두 경로가 갈라져 있지 않다(반쪽 동작 방지)",
      SM.AVG_DOWN_INDEX_DROP_PCT == SM.RESCUE_ADD_INDEX_DROP_PCT)
check("🔴 기존 INDEX_GUARD_THRESHOLD(-5%)보다 **훨씬 자주** 걸린다",
      THR > SM.INDEX_GUARD_THRESHOLD,
      "%s vs %s" % (THR, SM.INDEX_GUARD_THRESHOLD))
check("추가매수 두 경로가 켜져 있다(문턱이 도달 가능한 상태)",
      SM.AVG_DOWN_ENABLED and SM.RESCUE_ADD_ENABLED)

s = build(kosdaq=BAD_RATE)
check("헬퍼 _add_buy_index_blocked가 존재한다",
      hasattr(s, "_add_buy_index_blocked"))
check("  문턱 이하면 True", s._add_buy_index_blocked(THR) is True)
set_index(s, kosdaq=OK_RATE)
check("  문턱 위면 False", s._add_buy_index_blocked(THR) is False)
check("  threshold=None이면 항상 False(규칙 OFF)",
      s._add_buy_index_blocked(None) is False)

# ══════════════════════════════════════════════════════════════════
section("[2] 물타기 경계 — 문턱 '이하'가 차단")
# ══════════════════════════════════════════════════════════════════
for rate, want, lbl in ((0.0, True, "지수 0.00%"),
                        (OK_RATE, True, "지수 %.2f%% (문턱 위)" % OK_RATE),
                        (EDGE_RATE, False, "지수 %.2f%% (문턱 정각)" % EDGE_RATE),
                        (BAD_RATE, False, "지수 %.2f%% (문턱 아래)" % BAD_RATE),
                        (-3.52, False, "지수 -3.52% (08-18 코스닥 종가)")):
    s = build(kosdaq=rate)
    p = put(s)
    s.on_price_update("X", 9_700)                 # 원가 -3.0% = 물타기 트리거
    fired = len(buys(s)) == 1
    check("%s -> %s" % (lbl, "물타기" if want else "차단"),
          fired is want, "매수 %d건" % len(buys(s)))

s = build(kosdaq=BAD_RATE)
p = put(s)
s.on_price_update("X", 9_700)
check("차단 시 avg_down_done이 찍힌다(매 틱 재판정 안 함)",
      bool(p.get("avg_down_done")))

# ══════════════════════════════════════════════════════════════════
section("[3] 코스피/코스닥 중 나쁜 쪽을 본다")
# ══════════════════════════════════════════════════════════════════
for kp, kq, want, lbl in ((BAD_RATE, 5.0, False, "코스피만 -1.01% (코스닥 +5%)"),
                          (5.0, BAD_RATE, False, "코스닥만 -1.01% (코스피 +5%)"),
                          (OK_RATE, OK_RATE, True, "둘 다 문턱 위"),
                          (2.12, -1.16, False, "🔴 08-18 09:08 실측(코스피+2.12/코스닥-1.16)")):
    s = build(kospi=kp, kosdaq=kq)
    p = put(s)
    s.on_price_update("X", 9_700)
    fired = len(buys(s)) == 1
    check("%s -> %s" % (lbl, "물타기" if want else "차단"), fired is want)

# ══════════════════════════════════════════════════════════════════
section("[4] 🔴 그날 종료 래치 — 회복해도 다시 사지 않는다")
# ══════════════════════════════════════════════════════════════════
# 사용자 선택. 순간판정만 하면 08-18 코스닥이 09:04 -1.26% -> 09:10 -0.64%로
# 회복했으므로 일성건설이 09:08에 막혔다가 09:10에 그대로 매수돼 **무효**가 된다.
s = build(now_dt=at(9, 8), kosdaq=-1.16)
p = put(s, now_dt=at(9, 8))
s.on_price_update("X", 9_700)
check("09:08 지수 -1.16% -> 차단", len(buys(s)) == 0)
check("  avg_down_done 래치", bool(p.get("avg_down_done")))

set_now(s, at(9, 10))
set_index(s, kosdaq=-0.64)               # 실측 회복값
s.on_price_update("X", 9_700)
check("🔴 09:10 지수가 -0.64%로 회복해도 물타기하지 않는다",
      len(buys(s)) == 0, "매수 %d건" % len(buys(s)))
s.on_price_update("X", 9_650)            # 더 밀려도
set_now(s, at(9, 30))
set_index(s, kosdaq=+0.35)               # 실측 09:30~10:00 회복값
s.on_price_update("X", 9_700)
check("  09:30 +0.35%까지 회복해도 여전히 안 산다", len(buys(s)) == 0)

# 대조군 — 래치가 '지수 때문'인지 확인 (원래 살 수 있는 조건인가)
s2 = build(now_dt=at(9, 10), kosdaq=-0.64)
p2 = put(s2, now_dt=at(9, 10))
s2.on_price_update("X", 9_700)
check("🔴 대조군: 같은 시각·같은 지수(-0.64%)인데 래치가 없으면 산다",
      len(buys(s2)) == 1, "매수 %d건" % len(buys(s2)))

# ══════════════════════════════════════════════════════════════════
section("[5] rescue-add — 차단 시 관찰조차 시작하지 않고 손절")
# ══════════════════════════════════════════════════════════════════
_SV_AD = SM.AVG_DOWN_ENABLED
SM.AVG_DOWN_ENABLED = False              # 물타기가 손절보다 먼저 개입하므로 끈다
try:
    s = build(kosdaq=OK_RATE)
    p = put(s)
    feed(s, "X")
    s.on_price_update("X", 9_540)        # 손절선 첫 이탈
    check("지수 정상: 매도하지 않고 관찰 시작",
          not sells(s) and p.get("rescue_watch_until") is not None)

    s = build(kosdaq=BAD_RATE)
    p = put(s)
    feed(s, "X")
    s.on_price_update("X", 9_540)
    check("🔴 지수 -1.01%: 조건이 다 성립해도 관찰조차 시작하지 않는다",
          p.get("rescue_watch_until") is None)
    check("  대신 그냥 손절된다(안전측 수렴)", len(sells(s)) == 1,
          "매도 %d건" % len(sells(s)))
    check("  추가매수 주문 0건", len(buys(s)) == 0)
    check("  rescue_added가 찍히지 않는다", not p.get("rescue_added"))

    # 관찰 중에 지수가 무너지면?
    s = build(kosdaq=OK_RATE)
    p = put(s)
    feed(s, "X")
    s.on_price_update("X", 9_540)
    check("관찰 시작됨(지수 정상)", p.get("rescue_watch_until") is not None)
    set_index(s, kosdaq=BAD_RATE)
    s.on_price_update("X", 9_535)        # 반등 확증이 성립하는 가격
    check("🔴 관찰 중 지수가 무너지면 반등이 확증돼도 추가매수하지 않는다",
          len(buys(s)) == 0, "매수 %d건" % len(buys(s)))
    check("  손절로 수렴한다", len(sells(s)) == 1, "매도 %d건" % len(sells(s)))

    # ══════════════════════════════════════════════════════════════
    section("[6] 🔴 A/B 무효반증 — 규칙을 끄면 같은 입력에서 발동하는가")
    # ══════════════════════════════════════════════════════════════
    # 이 블록이 통과하지 않으면 [2][3][5]는 '원래부터 안 되던 것'을 본 것이고
    # 이번 변경은 무효다. 반드시 **같은 입력**으로 상수만 바꿔 비교한다.
    _sv_r = SM.RESCUE_ADD_INDEX_DROP_PCT
    s = build(kosdaq=BAD_RATE)
    p = put(s)
    feed(s, "X")
    s.on_price_update("X", 9_540)
    check("A(현행 -1.0%): rescue 차단 -> 손절",
          len(buys(s)) == 0 and len(sells(s)) == 1)
    SM.RESCUE_ADD_INDEX_DROP_PCT = None
    try:
        s = build(kosdaq=BAD_RATE)
        p = put(s)
        feed(s, "X")
        s.on_price_update("X", 9_540)
        check("🔴 B(규칙 OFF): 같은 입력에서 관찰이 시작된다 — A/B가 갈린다",
              p.get("rescue_watch_until") is not None and not sells(s),
              "매도 %d건" % len(sells(s)))
    finally:
        SM.RESCUE_ADD_INDEX_DROP_PCT = _sv_r
finally:
    SM.AVG_DOWN_ENABLED = _SV_AD

_sv_a = SM.AVG_DOWN_INDEX_DROP_PCT
s = build(kosdaq=BAD_RATE)
p = put(s)
s.on_price_update("X", 9_700)
check("A(현행 -1.0%): 물타기 차단", len(buys(s)) == 0)
SM.AVG_DOWN_INDEX_DROP_PCT = None
try:
    s = build(kosdaq=BAD_RATE)
    p = put(s)
    s.on_price_update("X", 9_700)
    check("🔴 B(규칙 OFF): 같은 입력에서 물타기가 나간다 — A/B가 갈린다",
          len(buys(s)) == 1, "매수 %d건" % len(buys(s)))
finally:
    SM.AVG_DOWN_INDEX_DROP_PCT = _sv_a

# ══════════════════════════════════════════════════════════════════
section("[7] 🔴 기존 INDEX_GUARD로는 대체 불가 — 그래서 이 규칙이 필요하다")
# ══════════════════════════════════════════════════════════════════
check("기존 가드는 11:00부터만 켜진다", SM.INDEX_GUARD_FROM == SM.time(11, 0),
      SM.INDEX_GUARD_FROM.strftime("%H:%M"))
check("🔴 추가매수 컷오프(10:00) < 기존 가드 시작(11:00) — 두 창이 안 겹친다",
      CUT < SM.INDEX_GUARD_FROM)
check("  => AVG_DOWN_BLOCK_ON_INDEX_GUARD는 지금 구조적으로 도달 불가",
      SM.AVG_DOWN_BLOCK_ON_INDEX_GUARD is True)   # 상수는 롤백용으로 남겨둔다

# 기존 가드가 절대 안 켜지는 조건(-3.5%는 -5%보다 얕고, 09:30은 11:00 이전)
s = build(now_dt=SAFE_T, kosdaq=-3.52)
check("  실증: 09:30 코스닥 -3.52%에서 기존 가드는 꺼져 있다",
      s._is_index_guard_active() is False)
check("  실증: 같은 상황에서 새 규칙은 켜진다",
      s._add_buy_index_blocked(THR) is True)
p = put(s)
s.on_price_update("X", 9_700)
check("🔴 => 08-18 같은 날 물타기가 실제로 막힌다", len(buys(s)) == 0)

# ══════════════════════════════════════════════════════════════════
section("[8] 시각 컷오프와 OR — 둘 중 하나만 걸려도 안 산다")
# ══════════════════════════════════════════════════════════════════
for nd, rate, want, lbl in (
        (SAFE_T, OK_RATE, True, "09:30 + 지수 정상 -> 산다"),
        (SAFE_T, BAD_RATE, False, "09:30 + 지수 -1.01% -> 지수가 막는다"),
        (at(10, 30), OK_RATE, False, "10:30 + 지수 정상 -> 시각이 막는다"),
        (at(10, 30), BAD_RATE, False, "10:30 + 지수 -1.01% -> 둘 다 막는다")):
    s = build(now_dt=nd, kosdaq=rate)
    p = put(s, now_dt=nd)
    s.on_price_update("X", 9_700)
    fired = len(buys(s)) == 1
    check(lbl, fired is want, "매수 %d건" % len(buys(s)))

# ══════════════════════════════════════════════════════════════════
section("[9] 신규 매수는 막지 않는다 / 손절은 살아 있다")
# ══════════════════════════════════════════════════════════════════
s = build(kosdaq=BAD_RATE)
check("🔴 지수 -1.01%에서도 신규매수 전면차단 사유는 뜨지 않는다",
      not s._entry_block_reason(), repr(s._entry_block_reason()))

_SV_AD = SM.AVG_DOWN_ENABLED
_SV_RS = SM.RESCUE_ADD_ENABLED
SM.AVG_DOWN_ENABLED = False
SM.RESCUE_ADD_ENABLED = False
try:
    s = build(kosdaq=BAD_RATE)
    p = put(s)
    s.on_price_update("X", 9_500)        # -5.0% = 손절선 아래
    check("🔴 지수 문턱이 손절을 막지 않는다", len(sells(s)) == 1,
          "매도 %d건" % len(sells(s)))
finally:
    SM.AVG_DOWN_ENABLED = _SV_AD
    SM.RESCUE_ADD_ENABLED = _SV_RS

# ══════════════════════════════════════════════════════════════════
section("[10] fail-open — 지수 조회 실패면 차단하지 않는다")
# ══════════════════════════════════════════════════════════════════
s = build(kosdaq=BAD_RATE)


class _Boom:
    host = "https://mock"

    def get_index_change_rate(self, sector_code="001"):
        raise RuntimeError("조회 실패")


s.api = _Boom()
s._market_rate_at = None
s._kospi_rate = 0.0
s._kosdaq_rate = 0.0
check("조회가 예외를 던져도 봇이 안 죽는다",
      s._add_buy_index_blocked(THR) is False)
p = put(s)
s.on_price_update("X", 9_700)
check("  데이터가 없으면 매매를 막지 않는다(기존 가드와 같은 관례)",
      len(buys(s)) == 1, "매수 %d건" % len(buys(s)))

# 직전 캐시가 살아 있으면 그 값으로 막는다
s = build(kosdaq=BAD_RATE)
s._add_buy_index_blocked(THR)            # 캐시 채움
s.api = _Boom()
s._market_rate_at = None
check("🔴 조회 실패 시 직전 캐시값이 유지돼 계속 막는다",
      s._add_buy_index_blocked(THR) is True,
      "코스닥 캐시 %.2f" % s._kosdaq_rate)

# ══════════════════════════════════════════════════════════════════
section("[11] 롤백 — 상수 한 줄로 08-18 오전 사양 복귀")
# ══════════════════════════════════════════════════════════════════
_sv_a, _sv_r = SM.AVG_DOWN_INDEX_DROP_PCT, SM.RESCUE_ADD_INDEX_DROP_PCT
SM.AVG_DOWN_INDEX_DROP_PCT = None
SM.RESCUE_ADD_INDEX_DROP_PCT = None
try:
    s = build(kosdaq=-3.52)
    p = put(s)
    s.on_price_update("X", 9_700)
    check("롤백 후: 지수 -3.52%에서도 물타기가 나간다(08-18 오전 동작)",
          len(buys(s)) == 1, "매수 %d건" % len(buys(s)))
    # ⚠️ avg_down_done은 여기서 **매수 성공 때문에** 찍힌다('1회만' 래치).
    #    즉 이 플래그만으로는 차단/체결을 구분할 수 없다 — 수량으로 본다.
    check("  실제로 동일 수량이 추가 매수됐다(차단 래치가 아니다)",
          buys(s)[0]["qty"] == 90, "추가 %d주" % buys(s)[0]["qty"])
finally:
    SM.AVG_DOWN_INDEX_DROP_PCT = _sv_a
    SM.RESCUE_ADD_INDEX_DROP_PCT = _sv_r

s = build(kosdaq=-3.52)
p = put(s)
s.on_price_update("X", 9_700)
check("복원 후: 다시 차단된다", len(buys(s)) == 0)

# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("통과 %d건 / 실패 %d건" % (len(PASS), len(FAIL)))
print("=" * 70)
if FAIL:
    for f in FAIL:
        print("  FAIL: %s" % f)
    sys.exit(1)
