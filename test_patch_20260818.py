# -*- coding: utf-8 -*-
"""2026-08-18 검증분 전용 스위트 — 분할 잔량 하한(1차 매도가).

  [1] 1차 매도가 기록 — `_execute_sell`의 `is_partial` 단일 창구
      분할매도 4경로(확정익절 / 동적캡 / VI 상단 / 반등소진)가 전부 이 함수를
      지나므로 여기서만 기록하면 된다. **첫 분할만** 기록해야 한다 —
      2차가 덮으면 하한이 계단식으로 내려가 보호가 통째로 사라진다.

  [2] 잔량 트레일 하한 — 발동가 = max(고점 x (1-트레일), 1차 매도가)
      근거는 성과가 아니라 산술이다. 3% 트레일이 1차가보다 높게 팔리려면
      고점이 1차가 대비 +3.09%(=1/0.97) 이상이어야 한다. 아니면 트레일은
      반드시 1차보다 싸게 판다.
      실측(08-13~18) 5건 중 3건이 그랬다:
         LB세미콘 1차 5,000 -> 고점 5,060(+1.20%) -> 4,905 (-1.90%)
         다날     1차 5,380 -> 고점 5,450(+1.30%) -> 5,280 (-1.86%)
         심텍     1차 106,300 -> 고점 109,800(+3.29%) -> 106,100 (-0.19%)
      고점이 +5.12%였던 STX그린로지스만 +1.86%로 이득 -> 하한 무영향이어야 한다.

  경계값은 int(가격*(1+비율))로 재지 않는다 — 부동소수 오차로 멀쩡한 코드가
  실패로 보인다(08-13, 08-15 두 세션 연속으로 밟았다).
  실물 `_net_rate`로 역산한다.

실행: PYTHONIOENCODING=utf-8 AUTOTRADER_TEST_LOG=1 python test_patch_20260818.py
"""
import os
import sys

os.environ.setdefault("AUTOTRADER_TEST_LOG", "1")

from datetime import datetime, timedelta                # noqa: E402

import core.strategy_manager as SM                      # noqa: E402
from core.phase1b_controller import Phase1BController   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "OK  " if cond else "FAIL"
    tail = (" -- " + detail) if detail else ""
    print("  %s | %s%s" % (mark, name, tail))


def section(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


class _Repo:
    rows, sells, updates, holdings_src = [], [], [], []

    @classmethod
    def find_holdings(cls):
        return list(cls.holdings_src)

    @classmethod
    def find_by_date(cls, d):
        return []

    @classmethod
    def insert_buy(cls, **kw):
        cls.rows.append(kw)
        return len(cls.rows)

    @classmethod
    def update_sell(cls, trade_id=None, *a, **kw):
        cls.sells.append(dict(kw, trade_id=trade_id))
        return True

    @classmethod
    def update(cls, rid, data):
        cls.updates.append(dict(data, id=rid))
        return True

    @classmethod
    def add(cls, **kw):
        cls.rows.append(kw)
        return len(cls.rows)

    @classmethod
    def mark_bought(cls, i):
        return True

    @classmethod
    def log(cls, *a, **kw):
        return True

    @classmethod
    def find_closed_by_substrategy(cls, s):
        return []


class _Theme:
    def __init__(self, *a, **kw):
        self.code_to_theme = {}
        self.leading_themes = []

    def fetch_themes_from_github(self):
        pass

    def start_auto_update(self, *a, **kw):
        pass

    def is_leading_theme_stock(self, code):
        return False


class _Rest:
    def get_index_change_rate(self, s="001"):
        return 0.0

    def get_orderable_amount(self):
        return 20_000_000

    def get_current_price(self, code):
        return 0

    def get_minute_candles(self, code, interval=1, count=1, base_date=None):
        return []

    def get_daily_candles(self, code, base_dt=None, count=60):
        return []

    def get_basic_quote(self, code):
        return None

    def get_stock_change_rate(self, code):
        return 3.0


class _OrderMgr:
    def __init__(self):
        self.orders = []

    def buy(self, code, qty, price=0, sizing="R", exit_strategy="R", *a, **kw):
        self.orders.append({"side": "buy", "code": code, "qty": qty, "price": price})
        return {"success": True, "price": price or 10_000, "quantity": qty}

    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"side": "sell", "code": code, "qty": qty, "price": price})
        return {"success": True, "price": price or 10_000, "quantity": qty}


NOW = datetime(2026, 8, 18, 10, 0, 0)
BUY, QTY = 10_000, 100


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


def put(st, qty=QTY, code="B"):
    st._prev_closes[code] = BUY * 0.95
    st._opening_prices[code] = BUY
    st._cond_names[code] = "주도주상위"
    st.holdings[code] = {
        "trade_id": 1, "stock_code": code, "stock_name": code,
        "buy_price": BUY, "buy_quantity": qty, "qty": qty,
        "buy_time": NOW - timedelta(minutes=30),
        "warmup_until": NOW - timedelta(seconds=1),
        "sub_strategy": "1A", "strategy_phase": "1A",
        "origin_price": BUY, "lowest_price": BUY, "highest_price": BUY,
        "stop_rate": -0.045,
    }
    return st.holdings[code]


def price_for_net(st, target_net, buy=BUY):
    """실물 `_net_rate`로 목표 순수익률의 최소 정수 가격을 역산.

    int(buy*(1+net)) 곱셈으로 재면 부동소수 오차로 경계가 어긋난다.
    """
    p = int(buy * (1 + target_net))
    guard = 0
    while st._net_rate(buy, p) < target_net and guard < 100_000:
        p += 1
        guard += 1
    return p


def sells(st):
    return [o for o in st.order_manager.orders if o["side"] == "sell"]


def reasons():
    return " ".join(str(x.get("exit_reason") or "") for x in _Repo.sells)


def make_partial(st, code="B"):
    """실물 청산 사다리로 확정익절 50% 분할을 실제로 낸다.

    반환: (포지션, 1차 매도가)
    """
    p1 = price_for_net(st, SM.FLAT_TP_RATE + 0.0005)
    st.on_price_update(code, p1)
    return st.holdings.get(code), p1


# ══════════════════════════════════════════════════════════════
# 🔴 롤백 컨텍스트 — 이 기능은 현재 **OFF**다(2026-08-18 저녁 보류).
#    무효 반증에서 '잔량이 트레일까지 살아남는다'는 전제가 깨졌다:
#    분할 잔량이 1차 매도가 아래로 내려가기까지 같은 초가 91.8%라,
#    켜면 사실상 전량 매도가 된다(BREAKEVEN_EXIT_PARTIAL=False와 동치).
#    그래도 배선 테스트는 지우지 않는다 — 지우면 되살릴 때 검증이 없다
#    (08-10 교훈). 아래 [1]~[6]은 ON으로 켠 상태에서 배선을 확인한다.
# ══════════════════════════════════════════════════════════════
_SV_FLOOR = SM.PARTIAL_EXIT_FLOOR_AT_FIRST
SM.PARTIAL_EXIT_FLOOR_AT_FIRST = True

# ══════════════════════════════════════════════════════════════
section("[1] 1차 매도가 기록 — 단일 창구 / 첫 분할만")
# ══════════════════════════════════════════════════════════════
s = build()
put(s)
pos, p1 = make_partial(s)
check("확정익절 분할이 실제로 나갔다 (경로 도달 대조군)",
      pos is not None and pos.get("partial_exited") is True,
      "잔량 %s주" % (pos.get("qty") if pos else "청산됨"))
check("분할 시 partial_exit_price가 1차 매도가로 기록된다",
      pos is not None and float(pos.get("partial_exit_price") or 0) == float(p1),
      "기록 %s vs 1차가 %s" % (pos.get("partial_exit_price") if pos else None, p1))

s2 = build()
put(s2)
pos2, p1_2 = make_partial(s2)
assert pos2 is not None, "2차 분할 시나리오에서 포지션이 이미 청산됐다"
_before = pos2.get("partial_exit_price")
s2._execute_sell("B", int(p1_2 * 0.9), "테스트 2차 분할", sell_qty=1)
check("2차 분할이 partial_exit_price를 덮지 않는다 (계단식 하락 방지)",
      float(s2.holdings["B"].get("partial_exit_price")) == float(_before),
      "%s -> %s" % (_before, s2.holdings["B"].get("partial_exit_price")))

# ══════════════════════════════════════════════════════════════
section("[2] 하한 경계 — 현재가 == 1차가 / 1차가 +1원")
# ══════════════════════════════════════════════════════════════
s = build()
put(s)
pos, p1 = make_partial(s)
n0 = len(sells(s))
s.on_price_update("B", p1 + 1)
check("1차가 +1원에서는 잔량이 안 나간다", len(sells(s)) == n0,
      "매도 %d건" % (len(sells(s)) - n0))
# 🔴 이 패치의 핵심 경계. 1차 분할은 **바로 이 가격에서** 났으므로 `<=`로
#    구현하면 같은 가격 틱에서 잔량이 즉시 털린다 — 08-12에 VI 상단에서
#    잡았던 "분할 직후 다음 틱 전량청산" 결함의 재현이다.
#    실제로 이 패치 첫 판이 `<=`였고 기존 스위트 3개가 이걸 잡아냈다.
for _ in range(5):
    s.on_price_update("B", p1)
check("현재가 == 1차가 연속 5틱에도 잔량이 살아남는다 (즉시 털림 방지)",
      len(sells(s)) == n0 and "B" in s.holdings,
      "매도 %d건 / 잔량 %s" % (len(sells(s)) - n0, s.holdings.get("B", {}).get("qty")))
s.on_price_update("B", p1 - 1)
check("1차가보다 1원 아래에서 잔량이 나간다 (strict <)", len(sells(s)) > n0,
      "매도 %d건" % (len(sells(s)) - n0))

# ══════════════════════════════════════════════════════════════
section("[3] LB세미콘형(고점 +1.20%) — 하한이 트레일을 이긴다")
# ══════════════════════════════════════════════════════════════
s = build()
put(s)
pos, p1 = make_partial(s)
peak = int(p1 * 1.012)
s.on_price_update("B", peak)
trail_px = peak * (1.0 - SM.PARTIAL_EXIT_TRAIL)
check("전제: 트레일 발동가가 1차가보다 아래다 (하한이 의미 있는 구간)",
      trail_px < p1, "트레일가 %.0f < 1차가 %s" % (trail_px, p1))
n0 = len(sells(s))
s.on_price_update("B", p1 - 1)
check("1차가 아래로 내려오면 즉시 잔량 청산", len(sells(s)) > n0,
      "%d건" % (len(sells(s)) - n0))
check("사유에 '하한' 표기가 붙는다", "하한" in reasons(), reasons()[-70:])

# ══════════════════════════════════════════════════════════════
section("[4] STX형(고점 +5.12%) — 트레일이 이긴다, 하한 무영향")
# ══════════════════════════════════════════════════════════════
s = build()
put(s)
pos, p1 = make_partial(s)
# 트레일이 1차가보다 **위에서** 발동하려면 고점 >= 1차가/(1-트레일) 이어야 한다.
# 그런데 그 지점의 순수익은 이미 +5%대라 익절캡(6%)까지 여유가 1%p도 안 된다.
#   -> 확정익절(+2%)과 익절캡(6%) 사이에서 "트레일이 1차가보다 위에서 발동하는"
#      창은 구조적으로 매우 좁다. 이것 자체가 하한을 넣는 근거이기도 하다.
# 여기서는 그 좁은 창을 정확히 겨냥한다(캡보다 아래여야 트레일이 시험된다).
cap_px = price_for_net(s, SM.TAKE_PROFIT_CAP)
peak = int(p1 / (1.0 - SM.PARTIAL_EXIT_TRAIL)) + 20
check("전제: 고점이 익절캡 미만이라 트레일이 실제로 시험된다",
      peak < cap_px, "고점 %d < 캡가 %d" % (peak, cap_px))
s.on_price_update("B", peak)
trail_px = peak * (1.0 - SM.PARTIAL_EXIT_TRAIL)
check("전제: 트레일 발동가가 1차가보다 위다", trail_px > p1,
      "트레일가 %.0f > 1차가 %s" % (trail_px, p1))
check("전제: 포지션이 아직 살아 있다 (캡에 안 걸렸다)", "B" in s.holdings)
n0 = len(sells(s))
s.on_price_update("B", int(trail_px) + 2)
check("트레일 직전에서는 안 나간다", len(sells(s)) == n0)
s.on_price_update("B", int(trail_px) - 1)
check("트레일 도달 시 나간다 (1차가보다 위에서)", len(sells(s)) > n0)
check("이 경우엔 '하한' 표기가 붙지 않는다 (트레일이 발동)",
      "하한" not in reasons(), reasons()[-70:])

# ══════════════════════════════════════════════════════════════
section("[5] A/B — 하한 ON/OFF가 실제로 갈리는가 (무효 반증)")
# ══════════════════════════════════════════════════════════════
_orig = SM.PARTIAL_EXIT_FLOOR_AT_FIRST
res = {}
for flag in (False, True):
    SM.PARTIAL_EXIT_FLOOR_AT_FIRST = flag
    s = build()
    put(s)
    pos, p1 = make_partial(s)
    s.on_price_update("B", int(p1 * 1.012))
    n0 = len(sells(s))
    s.on_price_update("B", p1 - 1)
    res[flag] = len(sells(s)) - n0
SM.PARTIAL_EXIT_FLOOR_AT_FIRST = _orig
check("OFF에서는 1차가 아래에서 안 판다 (08-18 이전 동작)",
      res[False] == 0, "매도 %d건" % res[False])
check("ON에서는 1차가 아래에서 판다 (새 동작)",
      res[True] >= 1, "매도 %d건" % res[True])
check("A/B 결과가 실제로 갈린다 (갈리지 않으면 이 변경은 무효다)",
      res[False] != res[True], "OFF %d건 vs ON %d건" % (res[False], res[True]))
check("롤백 상수가 원복됐다", SM.PARTIAL_EXIT_FLOOR_AT_FIRST is _orig)

# ══════════════════════════════════════════════════════════════
section("[6] 상방 무영향 · 하방 보호 · 비대상 포지션")
# ══════════════════════════════════════════════════════════════
s = build()
put(s)
pos, p1 = make_partial(s)
n0 = len(sells(s))
for mult in (1.005, 1.010, 1.015, 1.020, 1.025):
    s.on_price_update("B", int(p1 * mult))
check("계속 오르면 하한도 트레일도 발동하지 않는다 (상방 무영향)",
      len(sells(s)) == n0 and "B" in s.holdings,
      "매도 %d건 / 잔량 %s" % (len(sells(s)) - n0, s.holdings.get("B", {}).get("qty")))

s = build()
put(s)
pos, p1 = make_partial(s)
n0 = len(sells(s))
s.on_price_update("B", price_for_net(s, SM.STOP_LOSS_RATE) - 5)
check("손절은 하한과 무관하게 발동한다 (하방 보호 유지)",
      len(sells(s)) > n0, "매도 %d건 / %s" % (len(sells(s)) - n0, reasons()[-46:]))

s = build()
put(s)
n0 = len(sells(s))
s.on_price_update("B", BUY - 1)
check("분할 전 포지션엔 하한이 관여하지 않는다",
      len(sells(s)) == n0 and "B" in s.holdings)

s = build()
p = put(s)
p["partial_exited"] = True
p["trail_peak"] = int(BUY * 1.05)
p["qty"] = QTY // 2
n0 = len(sells(s))
s.on_price_update("B", int(BUY * 1.045))
check("하위호환: partial_exit_price 없으면 하한 미적용(트레일만)",
      len(sells(s)) == n0, "매도 %d건" % (len(sells(s)) - n0))
s.on_price_update("B", int(BUY * 1.05 * (1 - SM.PARTIAL_EXIT_TRAIL)) - 1)
check("하위호환: 트레일 자체는 정상 발동", len(sells(s)) > n0)

SM.PARTIAL_EXIT_FLOOR_AT_FIRST = _SV_FLOOR       # 롤백 컨텍스트 종료

# ══════════════════════════════════════════════════════════════
section("[7] 상수 불변식")
# ══════════════════════════════════════════════════════════════
check("PARTIAL_EXIT_FLOOR_AT_FIRST 존재", hasattr(SM, "PARTIAL_EXIT_FLOOR_AT_FIRST"))
check("현재 하한은 OFF다 (2026-08-18 저녁 보류 — 문서와 일치)",
      SM.PARTIAL_EXIT_FLOOR_AT_FIRST is False,
      "켜려면 반증의 91.8% 즉시청산 문제를 먼저 해결할 것")
check("하한 ON이면 트레일 폭 > 0 (상방 청산 경로 생존)",
      (not SM.PARTIAL_EXIT_FLOOR_AT_FIRST) or SM.PARTIAL_EXIT_TRAIL > 0,
      str(SM.PARTIAL_EXIT_TRAIL))
check("분할 기능 자체가 켜져 있다 (꺼지면 하한이 도달 불가)",
      SM.PARTIAL_EXIT_ENABLED, str(SM.PARTIAL_EXIT_ENABLED))
check("잔량 보호도 켜져 있다 (동적캡이 잔량을 채가면 하한이 무의미)",
      SM.PARTIAL_EXIT_REMAINDER_HOLD, str(SM.PARTIAL_EXIT_REMAINDER_HOLD))

print("\n" + "=" * 72)
print("통과 %d건 / 실패 %d건" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("   FAIL:", f)
print("=" * 72)
sys.exit(1 if FAIL else 0)
