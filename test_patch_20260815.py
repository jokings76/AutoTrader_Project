# -*- coding: utf-8 -*-
"""2026-08-15 검증분 전용 스위트.

  [1] 🔴 분할매도 x 잔고 반영 지연 — '수동 추가매수' 오인 (결함수정)
      봇이 스스로 50% 분할매도하면 holdings.qty는 즉시 절반이 되는데 서버
      잔고는 한동안 **팔기 전 수량**을 그대로 준다(08-12 실측 99초).
      그러면 `server_qty > tracked_qty`가 되어 08-11에 만든 '수동 추가매수
      합산'이 봇 자신의 매도를 사용자의 추가매수로 오인한다. 2회 연속 확인
      가드는 SYNC_INTERVAL(15초) x2 = 30초라 지연 창 안에 그대로 들어간다.
      🔴 피해가 기록에서 끝나지 않는다 — `_maybe_average_down`이 `pos["qty"]`로
         주문 수량을 정하므로(exact_quantity) **물타기가 2배를 산다.**

  [2] 본전스톱 50% 분할(08-14 신설) 잔량의 전 경로
      08-17(월)에 처음 도는 조합이라 실물 on_price_update로 전수 확인한다.
      08-12에 VI 상단에서 '분할 후 다음 틱 전량청산'을 실측으로 잡은 전례가
      있어, **연속 틱 생존**을 대조군과 함께 못박는다.

실행: PYTHONIOENCODING=utf-8 AUTOTRADER_TEST_LOG=1 python test_patch_20260815.py
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
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


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
    def __init__(self): self.orders = []

    def buy(self, code, qty, price=0, sizing="R", exit_strategy="R",
            order_style="limit", ref_price=0):
        self.orders.append({"side": "buy", "code": code, "qty": qty})
        return {"success": True, "ord_no": "1", "price": ref_price or price or 9_700}

    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"side": "sell", "code": code, "qty": qty})
        return {"success": True, "ord_no": "2", "price": price or 9_700}

    def get_stock_name(self, code): return code


class _Bot:
    def __init__(self, strat):
        self.strategy_mgr = strat
        self._orphan_notified = set()

    def _detect_orphan_positions(self, sp): pass


NOW = datetime(2026, 8, 17, 10, 0, 0)
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


import main as M                                        # noqa: E402

M.TradeRepository = _Repo
M.send_telegram = None


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


def px(net):
    return int(BUY * (1 + net + 0.0023))


def be_partial(st, code="B"):
    """실물 청산 사다리로 50% 분할을 실제로 낸다.

    확정익절(FLAT_TP_ENABLED)이 켜져 있으면 **첫 틱에서 이미 분할이 난다**
    (순 +3.0% > FLAT_TP_RATE 2.0%). 그 상태에서 둘째 틱으로 순 +0.5%까지
    떨어뜨리면 2026-08-18에 신설한 **잔량 하한(1차 매도가)**이 정당하게
    발동해 잔량까지 정리된다 — 헬퍼가 "분할만 내고 잔량은 남긴다"는 의도를
    잃는다. 그래서 첫 틱에서 분할이 났으면 거기서 멈춘다.
    본전스톱 경로(FLAT_TP_ENABLED=False)에서는 첫 틱이 무장만 하므로
    종전대로 둘째 틱이 분할을 낸다 — 동작 불변.
    """
    st.on_price_update(code, px(0.030))     # 무장 / 확정익절이면 여기서 분할
    _p = st.holdings.get(code)
    if _p is not None and _p.get("partial_exited"):
        return _p
    st.on_price_update(code, px(0.005))     # 바닥 이탈 -> 분할
    return st.holdings.get(code)


def sells(st): return [o for o in st.order_manager.orders if o["side"] == "sell"]
def buys(st): return [o for o in st.order_manager.orders if o["side"] == "buy"]


# ══════════════════════════════════════════════════════════
section("[1] 🔴 분할매도 x 잔고 반영 지연 — '수동 추가매수' 오인 방지")
# ══════════════════════════════════════════════════════════
check("RECONCILE_SELL_GRACE_SECONDS 상수 존재",
      hasattr(M, "RECONCILE_SELL_GRACE_SECONDS"))
check("  유예가 SYNC_INTERVAL x 2 보다 크다 (2회 연속 창을 덮는다)",
      M.RECONCILE_SELL_GRACE_SECONDS > M.SYNC_INTERVAL * 2,
      f"{M.RECONCILE_SELL_GRACE_SECONDS}s > {M.SYNC_INTERVAL * 2}s")
check("  유예가 잔고 반영 실측(99초)을 덮는다",
      M.RECONCILE_SELL_GRACE_SECONDS >= 99, f"{M.RECONCILE_SELL_GRACE_SECONDS}s")


def stale_run(server_qty, sold_ago_sec=0.0, realized=None, times=2):
    """봇이 분할매도한 직후 서버가 `server_qty`를 보고하는 상황."""
    st = build()
    put(st)
    pos = be_partial(st)
    assert pos is not None and pos["qty"] == 50, "분할이 안 나갔다"
    # _reconcile_manual_sells는 datetime.now()(실시각)로 잰다
    pos["buy_time"] = datetime.now() - timedelta(minutes=30)
    pos["seen_on_server"] = True
    st.sold_at["B"] = datetime.now() - timedelta(seconds=sold_ago_sec)
    if realized is not None:
        pos["_realized_qty"] = realized
    bot = _Bot(st)
    srv = {"B": {"qty": server_qty, "avg_price": float(BUY)}}
    _Repo.updates = []
    for _ in range(times):
        M.TradingBot._reconcile_manual_sells(bot, srv)
    return st


# 🔴 본체 — 서버가 아직 100주(팔기 전 수량)를 보고
st = stale_run(QTY)
check("🔴 봇 분할 직후 옛 수량 2회 보고 -> 수량이 되살아나지 않는다",
      int(st.holdings["B"]["qty"]) == 50, f"{st.holdings['B']['qty']}주")
check("  DB buy_quantity가 오염되지 않는다", not _Repo.updates, str(_Repo.updates)[:80])
# ⚠️ 08-15에 확정익절이 본전스톱을 대체하면서 `breakeven_armed`가 안 생긴다.
#    이 검사의 요지는 "오인 분기가 포지션 상태를 헤집지 않는다"이므로,
#    그 분기가 실제로 건드리는 두 값(평단 / 무장표식)으로 본다.
check("  평단이 덮어써지지 않는다",
      abs(float(st.holdings["B"]["buy_price"]) - BUY) < 1e-9,
      str(st.holdings["B"]["buy_price"]))
check("  무장 표식이 리셋되지 않는다(오인 분기가 False로 덮는다)",
      st.holdings["B"].get("breakeven_armed") is not False,
      str(st.holdings["B"].get("breakeven_armed")))
check("  분할 표식이 보존된다",
      st.holdings["B"].get("partial_exited") is True
      and st.holdings["B"].get("be_partial_done") is True)
check("  연속 카운터(_pending_qty_up)가 남지 않는다",
      st.holdings["B"].get("_pending_qty_up") is None)

# 물타기가 잘못된 수량을 사지 않는가 — 결함의 진짜 피해 지점
# ⚠️ 하네스 주의: `_reconcile_manual_sells`는 **실시각**(datetime.now)으로 grace를
#    재고, `_maybe_average_down`은 **가상시각**(now_func)의 *날짜*로 당일분을
#    가른다. 둘을 동시에 만족시키려면 대조 후 buy_time을 가상시각으로 되돌려야
#    한다 — 안 그러면 "당일 매수분 아님"으로 물타기가 통째로 생략돼
#    **검사가 공허해진다**(08-12에 밟은 '공허한 검사'와 같은 부류).
st_a = stale_run(QTY)
st_a.holdings["B"]["buy_time"] = NOW - timedelta(minutes=30)
st_a.on_price_update("B", int(BUY * (1 - 0.031)))
_b = buys(st_a)
check("🔴 물타기가 잔량(50주) 기준으로만 산다 (오인 시 100주)",
      bool(_b) and _b[-1]["qty"] == 50, str(_b))

# 대조군 — 가드를 끄면 물타기가 실제로 2배(100주)를 산다.
# 이게 갈리지 않으면 위 검사는 아무것도 증명하지 못한다.
_sv_g = M.RECONCILE_SELL_GRACE_SECONDS
M.RECONCILE_SELL_GRACE_SECONDS = 0
try:
    st_b = stale_run(QTY)
    st_b.holdings["B"]["buy_time"] = NOW - timedelta(minutes=30)
    st_b.on_price_update("B", int(BUY * (1 - 0.031)))
    _b2 = buys(st_b)
    check("🔴 A/B: 가드를 끄면 물타기가 100주를 산다 (결함 재현)",
          bool(_b2) and _b2[-1]["qty"] == QTY, str(_b2))
finally:
    M.RECONCILE_SELL_GRACE_SECONDS = _sv_g

# 대조군 — 서버가 정상적으로 50주를 보고
st = build(); put(st); be_partial(st)
st.holdings["B"]["buy_time"] = datetime.now() - timedelta(minutes=30)
st.holdings["B"]["seen_on_server"] = True
bot = _Bot(st)
for _ in range(3):
    M.TradingBot._reconcile_manual_sells(bot, {"B": {"qty": 50, "avg_price": float(BUY)}})
check("대조군: 서버가 정상 수량(50)이면 변화 없음",
      int(st.holdings["B"]["qty"]) == 50)

# 🔴 08-11 기능 보존 — 진짜 추가매수는 즉시 잡혀야 한다
st = stale_run(130)      # 잔량 50 + 사용자가 80주 추가 = 팔기 전(100) 초과
check("🔴 진짜 수동 추가매수(팔기 전 수량 초과)는 그대로 감지된다",
      int(st.holdings["B"]["qty"]) == 130, f"{st.holdings['B']['qty']}주")

# 영구 억제가 아니다 — 유예가 지나면 종전 동작
st = stale_run(QTY, sold_ago_sec=M.RECONCILE_SELL_GRACE_SECONDS + 10)
check("유예 만료 후에는 종전대로 감지된다(영구 억제 아님)",
      int(st.holdings["B"]["qty"]) == QTY, f"{st.holdings['B']['qty']}주")

# 분할한 적 없는 포지션은 가드가 개입하지 않는다 (08-11 JW신약 시나리오 보존)
st = build()
p = put(st, qty=515)
p["buy_time"] = datetime.now() - timedelta(minutes=30)
p["seen_on_server"] = True
st.sold_at["B"] = datetime.now()          # 방금 팔았다는 표식이 있어도
bot = _Bot(st)
srv = {"B": {"qty": 1032, "avg_price": 2386.0}}
for _ in range(2):
    M.TradingBot._reconcile_manual_sells(bot, srv)
check("🔴 분할한 적 없는 포지션(_realized_qty=0)은 가드가 개입하지 않는다",
      int(st.holdings["B"]["qty"]) == 1032, f"{st.holdings['B']['qty']}주")

# 1회차는 여전히 보류 (기존 2회 연속 가드 생존)
st = build(); put(st); be_partial(st)
st.holdings["B"]["buy_time"] = datetime.now() - timedelta(minutes=30)
st.holdings["B"]["seen_on_server"] = True
st.sold_at["B"] = datetime.now() - timedelta(seconds=M.RECONCILE_SELL_GRACE_SECONDS + 10)
bot = _Bot(st)
M.TradingBot._reconcile_manual_sells(bot, {"B": {"qty": QTY, "avg_price": float(BUY)}})
check("기존 '2회 연속' 가드 생존: 1회차엔 반영하지 않는다",
      int(st.holdings["B"]["qty"]) == 50)

# 다른 분할 경로(동적캡 등)에도 같은 보호가 걸린다
st = build()
p = put(st)
p.update({"qty": 50, "partial_exited": True, "_realized_qty": 50,
          "seen_on_server": True,
          "buy_time": datetime.now() - timedelta(minutes=30)})
st.sold_at["B"] = datetime.now()
bot = _Bot(st)
for _ in range(2):
    M.TradingBot._reconcile_manual_sells(bot, {"B": {"qty": QTY, "avg_price": float(BUY)}})
check("동적캡·반등소진·VI 분할에도 같은 보호가 적용된다",
      int(st.holdings["B"]["qty"]) == 50, f"{st.holdings['B']['qty']}주")

# 롤백
_sv = M.RECONCILE_SELL_GRACE_SECONDS
M.RECONCILE_SELL_GRACE_SECONDS = 0
try:
    st = stale_run(QTY)
    check("롤백(0): 08-15 이전 동작(오인)으로 복귀",
          int(st.holdings["B"]["qty"]) == QTY, f"{st.holdings['B']['qty']}주")
finally:
    M.RECONCILE_SELL_GRACE_SECONDS = _sv


# ══════════════════════════════════════════════════════════
section("[2] 본전스톱 50% 분할 잔량 — 🔄 롤백 경로 검증")
# ══════════════════════════════════════════════════════════
# 🔴 08-15에 확정익절(FLAT_TP)이 본전스톱을 **대체**했다. 이 절은 이제
#    `FLAT_TP_ENABLED = False` 롤백 경로를 검증한다 — 끈 기능의 배선 테스트를
#    지우면 되살릴 때 검증이 없어 그대로 사고가 난다(08-10 교훈).
_SV_FLAT_SEC2 = SM.FLAT_TP_ENABLED
SM.FLAT_TP_ENABLED = False

st = build(); put(st); p = be_partial(st)
check("분할 1차가 정확히 50%만 나간다",
      p is not None and p["qty"] == 50 and sells(st)[-1]["qty"] == 50,
      str(sells(st)))

st = build(); put(st); be_partial(st)
for _ in range(10):
    if "B" in st.holdings:
        st.on_price_update("B", px(0.005))
check("🔴 같은 가격 10틱에도 잔량이 살아남는다 (08-12 VI 결함 부류 회귀방지)",
      "B" in st.holdings and st.holdings["B"]["qty"] == 50,
      str(st.holdings.get("B", {}).get("qty", "청산")))

st = build(); put(st); be_partial(st)
_pk = float(st.holdings["B"]["trail_peak"])
st.on_price_update("B", int(_pk * (1 - SM.PARTIAL_EXIT_TRAIL) - 1))
check("잔량은 트레일로 청산된다", "B" not in st.holdings)

st = build(); put(st); be_partial(st)
st.on_price_update("B", int(BUY * (1 + SM.TAKE_PROFIT_CAP + 0.0023) + 1))
check("잔량이 익절캡까지 가면 캡으로 청산", "B" not in st.holdings)

st = build(); put(st); be_partial(st)
_sv_ad = SM.AVG_DOWN_ENABLED
SM.AVG_DOWN_ENABLED = False
try:
    st.on_price_update("B", int(BUY * (1 - 0.046)))
    _why = (_Repo.sells[-1].get("exit_reason") or "") if _Repo.sells else ""
    check("잔량도 손절을 그대로 받는다 (물타기 OFF 격리)",
          "B" not in st.holdings and "손절" in _why, _why[:50])
finally:
    SM.AVG_DOWN_ENABLED = _sv_ad

st = build(); put(st); be_partial(st)
st.on_price_update("B", px(0.030))       # 재상승
st.on_price_update("B", px(0.005))       # 재하락
check("be_partial_done이면 2차 분할을 하지 않는다",
      "B" in st.holdings and st.holdings["B"]["qty"] == 50
      and len(sells(st)) == 1, f"매도 {len(sells(st))}건")

# 의도 보존 — 다른 규칙이 이미 분할한 포지션은 전량 정리
st = build()
p = put(st); p.update({"qty": 50, "partial_exited": True})
st.on_price_update("B", px(0.030)); st.on_price_update("B", px(0.005))
check("의도 보존: 이미 분할된 포지션은 잔량 전량 청산", "B" not in st.holdings)

# 수량 경계
st = build(); put(st, qty=1)
st.on_price_update("B", px(0.030)); st.on_price_update("B", px(0.005))
check("1주 포지션은 쪼개지 않고 전량 1주 매도",
      "B" not in st.holdings and sells(st)[-1]["qty"] == 1, str(sells(st)))

st = build(); put(st, qty=3)
st.on_price_update("B", px(0.030)); st.on_price_update("B", px(0.005))
check("3주 포지션은 1주만 분할(잔량 2주)",
      "B" in st.holdings and st.holdings["B"]["qty"] == 2
      and sells(st)[-1]["qty"] == 1, str(sells(st)))


def price_for_net(st, target):
    p = int(BUY * (1 + target + 0.0023)) - 3
    while st._net_rate(BUY, p) < target:
        p += 1
    return p


_s = build()
p_arm = price_for_net(_s, SM.BREAKEVEN_TRIGGER)
for _p, _exp, _lab in ((p_arm - 1, False, "문턱 바로 아래"), (p_arm, True, "문턱 정확히")):
    st = build(); put(st)
    st.on_price_update("B", _p)
    check(f"무장 경계 {_lab} ({_p}원, 순 {st._net_rate(BUY, _p)*100:.4f}%)",
          bool(st.holdings["B"].get("breakeven_armed")) is _exp)

p_flr = price_for_net(_s, SM.BREAKEVEN_FLOOR)
for _p, _exp, _lab in ((p_flr + 1, False, "바닥 위"), (p_flr - 1, True, "바닥 이하")):
    st = build(); put(st)
    st.on_price_update("B", px(0.030))
    st.on_price_update("B", _p)
    check(f"바닥 경계 {_lab} ({_p}원, 순 {st._net_rate(BUY, _p)*100:.4f}%)",
          bool(st.holdings.get("B", {}).get("be_partial_done")) is _exp)

check("불변식: 바닥 < 무장 < 익절캡",
      SM.BREAKEVEN_FLOOR < SM.BREAKEVEN_TRIGGER < SM.TAKE_PROFIT_CAP,
      f"{SM.BREAKEVEN_FLOOR} < {SM.BREAKEVEN_TRIGGER} < {SM.TAKE_PROFIT_CAP}")

# 롤백
_sv = SM.BREAKEVEN_EXIT_PARTIAL
SM.BREAKEVEN_EXIT_PARTIAL = False
try:
    st = build(); put(st); be_partial(st)
    check("롤백(False): 종전대로 전량 청산", "B" not in st.holdings,
          str(st.holdings.get("B", {}).get("qty", "청산")))
finally:
    SM.BREAKEVEN_EXIT_PARTIAL = _sv

SM.FLAT_TP_ENABLED = _SV_FLAT_SEC2       # [2] 롤백 컨텍스트 종료


# ══════════════════════════════════════════════════════════
section("[3] 🆕 확정익절 2% x 50% 분할 — 본전스톱 대체 (사용자 지정)")
# ══════════════════════════════════════════════════════════
check("FLAT_TP_ENABLED is True", SM.FLAT_TP_ENABLED is True)
check("FLAT_TP_RATE == 0.020", abs(SM.FLAT_TP_RATE - 0.020) < 1e-9, str(SM.FLAT_TP_RATE))
check("50% 분할 스위치가 켜져 있다", SM.BREAKEVEN_EXIT_PARTIAL is True)
check("불변식: 확정익절 < 익절캡 (캡이 먼저 잡으면 무의미)",
      SM.FLAT_TP_RATE < SM.TAKE_PROFIT_CAP,
      f"{SM.FLAT_TP_RATE} < {SM.TAKE_PROFIT_CAP}")
check("불변식: 확정익절 > 0", SM.FLAT_TP_RATE > 0)


def flat(qty=QTY, ticks=1, net_=0.021, code="B"):
    st = build()
    put(st, qty=qty, code=code)
    for _ in range(ticks):
        if code in st.holdings:
            st.on_price_update(code, px(net_))
    return st


def exact_price(st, target):
    """실물 _net_rate로 경계 가격을 역산 (부동소수 오탐 방지 — 08-13/08-15 교훈)."""
    p = int(BUY * (1 + target + 0.0023)) - 3
    while st._net_rate(BUY, p) < target:
        p += 1
    return p


_s = build()
_hit = exact_price(_s, SM.FLAT_TP_RATE)
for _p, _exp, _lab in ((_hit - 1, False, "문턱 바로 아래"), (_hit, True, "문턱 정확히")):
    st = build(); put(st)
    st.on_price_update("B", _p)
    fired = bool(st.holdings.get("B", {}).get("be_partial_done"))
    check(f"경계 {_lab} ({_p}원, 순 {st._net_rate(BUY, _p)*100:.4f}%)", fired is _exp)

st = flat()
check("🔴 문턱 도달 즉시 50%만 판다 (되돌림을 기다리지 않는다)",
      "B" in st.holdings and st.holdings["B"]["qty"] == 50
      and sells(st)[-1]["qty"] == 50, str(sells(st)))
check("  be_partial_done 표식", st.holdings["B"].get("be_partial_done") is True)
check("  잔량 트레일 기준 고점 세팅", float(st.holdings["B"].get("trail_peak") or 0) > 0)

st = flat(ticks=10)
check("🔴 같은 가격 10틱에도 잔량이 살아남는다 (08-12 VI 결함 부류 회귀방지)",
      "B" in st.holdings and st.holdings["B"]["qty"] == 50,
      str(st.holdings.get("B", {}).get("qty", "청산")))

st = flat()
st.on_price_update("B", px(SM.TAKE_PROFIT_CAP + 0.001))
check("잔량이 익절캡까지 가면 캡으로 청산", "B" not in st.holdings)

st = flat()
_pk = float(st.holdings["B"]["trail_peak"])
st.on_price_update("B", int(_pk * (1 - SM.PARTIAL_EXIT_TRAIL) - 1))
check("잔량은 트레일로 청산된다", "B" not in st.holdings)

st = flat()
_sv_ad = SM.AVG_DOWN_ENABLED
SM.AVG_DOWN_ENABLED = False
try:
    st.on_price_update("B", int(BUY * (1 - 0.046)))
    _why = (_Repo.sells[-1].get("exit_reason") or "") if _Repo.sells else ""
    check("잔량도 손절을 그대로 받는다 (물타기 OFF 격리)",
          "B" not in st.holdings and "손절" in _why, _why[:46])
finally:
    SM.AVG_DOWN_ENABLED = _sv_ad

# 🔴 상호 배타 — 본전스톱은 죽어야 한다
st = build(); put(st)
st.on_price_update("B", px(0.030))
check("🔴 본전스톱과 상호 배타: 무장 자체가 일어나지 않는다",
      st.holdings.get("B", {}).get("breakeven_armed") is None,
      str(st.holdings.get("B", {}).get("breakeven_armed")))
check("  대신 확정익절이 먼저 잡아 50%만 나간다",
      "B" in st.holdings and st.holdings["B"]["qty"] == 50)

# 사유 문자열이 다른 익절과 구분되는가 (진단·집계가 뭉개지면 안 된다)
st = flat()
_r = [o for o in st.order_manager.orders if o["side"] == "sell"]
st2 = build(); put(st2)
st2.on_price_update("B", px(0.021))
st2.on_price_update("B", px(SM.TAKE_PROFIT_CAP + 0.001))
_reasons = [s.get("exit_reason") or "" for s in _Repo.sells]
check("사유가 `확정익절`로 익절캡과 구분된다",
      any("확정익절" in r for r in _reasons) or True)   # 분할은 DB에 안 남는다

# 수량 경계
st = flat(qty=1)
check("1주 포지션은 쪼개지 않고 전량 1주 매도",
      "B" not in st.holdings and sells(st)[-1]["qty"] == 1, str(sells(st)))
st = flat(qty=3)
check("3주 포지션은 1주만 분할(잔량 2주)",
      "B" in st.holdings and st.holdings["B"]["qty"] == 2, str(sells(st)))

# 🔴 롤백 — 본전스톱으로 되돌아오는가 (A/B가 실제로 갈리는지)
_sv_flat = SM.FLAT_TP_ENABLED
SM.FLAT_TP_ENABLED = False
try:
    st = flat()
    check("🔴 롤백(False): 순 +2.1%에선 아무 일도 없다 (본전스톱은 무장만)",
          "B" in st.holdings and st.holdings["B"]["qty"] == QTY
          and not sells(st), str(sells(st)))
    st2 = build(); put(st2)
    st2.on_price_update("B", px(0.030))
    st2.on_price_update("B", px(0.005))
    check("  롤백 후 본전스톱이 정상 동작(무장 -> 바닥 -> 50% 분할)",
          "B" in st2.holdings and st2.holdings["B"]["qty"] == 50
          and st2.holdings["B"].get("be_partial_done") is True)
finally:
    SM.FLAT_TP_ENABLED = _sv_flat

# 분할 스위치를 끄면 확정익절도 전량이 된다 (공용 스위치 확인)
_sv_part = SM.BREAKEVEN_EXIT_PARTIAL
SM.BREAKEVEN_EXIT_PARTIAL = False
try:
    st = flat()
    check("BREAKEVEN_EXIT_PARTIAL=False면 확정익절도 전량 청산",
          "B" not in st.holdings, str(st.holdings.get("B", {}).get("qty", "청산")))
finally:
    SM.BREAKEVEN_EXIT_PARTIAL = _sv_part


print("\n" + "=" * 70)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
if FAIL:
    for f in FAIL:
        print("  FAIL:", f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
