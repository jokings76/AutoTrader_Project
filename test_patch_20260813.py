# -*- coding: utf-8 -*-
"""2026-08-12 장마감 후 적용분 전용 스위트 (다음 거래일 08-13부터 도는 변경).

  [1] 🔴 수동 추가매수 합산 — `elif` 결함 수정
      08-11에 만든 기능이 `elif`라 **한 번도 실행된 적이 없었다**.
      실측: 08-12 JW신약 봇 515주 vs 실계좌 1,032주(517주 방치).
  [2] 진입 유효창 — 편입 후 MAX_ENTRY_AGE_SEC 초과면 매수 포기
      근거: 8일 104건, 30분 초과 14건이 MFE +1.79%(절반이 못 먹음), 5/5일 우세.
  [3] VI 상단 확정매도 50% 분할 (잔량은 트레일)
      근거: 8일 9건, 78%가 판 뒤 +3% 이상 더 감.
  [4] 분할 잔량 보호 — 동적캡 재발동으로 잔량을 털지 않는다
      실측: 분할 1차 후 44~55초 만에 재발동해 전량청산이 됐다.
  [5] 편입가 앵커 — 되돌림 2차 트랜치를 편입가로 (downside 0 가드 포함)

실행: PYTHONIOENCODING=utf-8 AUTOTRADER_TEST_LOG=1 python test_patch_20260813.py
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


NOW = datetime(2026, 8, 13, 10, 0, 0)


def build(now_dt=NOW):
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


def put(s, code="X", buy=10_000, qty=100, now_dt=None):
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


def sells_of(s): return [o for o in s.order_manager.orders if o["side"] == "sell"]


# ══════════════════════════════════════════════════════════
section("[1] 🔴 수동 추가매수 합산 — elif 결함 수정")
# ══════════════════════════════════════════════════════════
import main as M                                        # noqa: E402

src = inspect.getsource(M.TradingBot._reconcile_manual_sells)
seen_i = src.find('pos["seen_on_server"] = True')
add_i = src.find("server_qty > tracked_qty")
check("두 분기가 모두 존재", seen_i > 0 and add_i > 0)
check("🔴 추가매수 분기가 elif가 아니다 (도달 가능)",
      "elif server_qty > tracked_qty" not in src,
      "elif면 server_qty>0에서 항상 첫 분기로 빠져 영원히 실행되지 않는다")

# 실제 동작 — 08-12 JW신약 시나리오 재현
_sv_repo = getattr(M, "TradeRepository", None)
_sv_tel = getattr(M, "send_telegram", None)
M.TradeRepository = _Repo
M.send_telegram = None
_Repo.updates = []


class _Bot:
    def __init__(self, strat):
        self.strategy_mgr = strat
        self._orphan_notified = set()

    def _detect_orphan_positions(self, sp):
        pass


s = build()
p = put(s, code="067290", buy=2_413, qty=515)
# ⚠️ _reconcile_manual_sells는 datetime.now()(**실제 시각**)로 grace를 잰다.
#    픽스처의 가상 시각(NOW)을 쓰면 경과가 음수가 돼 grace에 걸린다
#    (CLAUDE.md: "리플레이는 반드시 실시간 기준 과거 시각으로 돌릴 것").
p["buy_time"] = datetime.now() - timedelta(minutes=30)
p["seen_on_server"] = True
bot = _Bot(s)
srv = {"067290": {"qty": 1032, "avg_price": 2386.0}}
# 2회 연속 같은 값이어야 반영된다(봇 자신의 분할 2차 오인 방지)
M.TradingBot._reconcile_manual_sells(bot, srv)
first = int(s.holdings["067290"]["qty"])
M.TradingBot._reconcile_manual_sells(bot, srv)
after = int(s.holdings["067290"]["qty"])

check("1회차엔 보류(오인 방지) — 수량 불변", first == 515, f"{first}주")
check("🔴 2회차에 수동 추가매수가 반영된다", after == 1032, f"{after}주")
check("  평단이 서버 값으로 갱신", abs(s.holdings["067290"]["buy_price"] - 2386.0) < 1,
      f"{s.holdings['067290']['buy_price']:,.0f}원")
check("  origin_price는 유지(최종손절 기준)",
      abs(float(s.holdings["067290"]["origin_price"]) - 2413) < 1)
check("  본전스톱 상태 재설정", s.holdings["067290"].get("breakeven_armed") is False)
check("  DB에도 수량·평단 반영", any(u.get("buy_quantity") == 1032 for u in _Repo.updates),
      str(_Repo.updates)[:80])

# 대조군 — 수량이 같으면 아무 일도 없어야 한다
s2 = build()
p2 = put(s2, code="AAA", buy=1_000, qty=100)
p2["buy_time"] = datetime.now() - timedelta(minutes=30)
p2["seen_on_server"] = True
b2 = _Bot(s2)
for _ in range(3):
    M.TradingBot._reconcile_manual_sells(b2, {"AAA": {"qty": 100, "avg_price": 1000.0}})
check("대조군: 수량 동일하면 변화 없음", int(s2.holdings["AAA"]["qty"]) == 100)

if _sv_repo is not None:
    M.TradeRepository = _sv_repo
M.send_telegram = _sv_tel


# ══════════════════════════════════════════════════════════
section("[2] 진입 유효창 — 편입 후 30분 초과면 매수 포기")
# ══════════════════════════════════════════════════════════
check("MAX_ENTRY_AGE_SEC == 1800", SM.MAX_ENTRY_AGE_SEC == 1800,
      str(getattr(SM, "MAX_ENTRY_AGE_SEC", "(없음)")))
check("숙성(하한) < 유효창(상한) — 창이 열려 있다",
      SM.MIN_ENTRY_DELAY_SEC < SM.MAX_ENTRY_AGE_SEC)

import time as _t
s = build()
now = _t.time()
for age, want_block in ((0, True), (29, True), (60, False), (1799, False),
                        (1801, True), (3600, True)):
    s._first_seen["Z"] = now - age
    r = s._entry_delay_reject("Z", now)
    blocked = r is not None
    lab = "차단" if want_block else "통과"
    why = ("숙성" if age < SM.MIN_ENTRY_DELAY_SEC else
           "유효창 만료" if age > SM.MAX_ENTRY_AGE_SEC else "-")
    check(f"편입 후 {age:>4d}초 -> {lab} ({why})", blocked is want_block,
          str(r)[:46] if r else "통과")

check("🔴 유효창 만료 사유가 숙성과 구분된다",
      "숙성" not in (s._entry_delay_reject("Z", now) or ""),
      str(s._entry_delay_reject("Z", now))[:50])

# 🔴 재편입해도 '첫 편입' 기준이어야 한다 (검증 전제와 일치)
s2 = build()
s2.prearm_candidate("R")
first_seen = s2._first_seen.get("R")
s2.prearm_candidate("R")            # 재편입
check("🔴 재편입해도 _first_seen이 갱신되지 않는다(첫 편입 기준)",
      s2._first_seen.get("R") == first_seen)

# 롤백
_sv = SM.MAX_ENTRY_AGE_SEC
SM.MAX_ENTRY_AGE_SEC = 0
try:
    s3 = build()
    s3._first_seen["Z"] = _t.time() - 7200
    check("롤백(0): 상한이 사라져 2시간 뒤에도 통과",
          s3._entry_delay_reject("Z") is None)
finally:
    SM.MAX_ENTRY_AGE_SEC = _sv


# ══════════════════════════════════════════════════════════
section("[3] VI 상단 확정매도 50% 분할")
# ══════════════════════════════════════════════════════════
check("VI_UPPER_EXIT_PARTIAL is True", SM.VI_UPPER_EXIT_PARTIAL is True)
check("VI 확정매도 자체는 켜져 있다", SM.VI_UPPER_EXIT_ENABLED is True)


def vi_case(partial):
    _s = SM.VI_UPPER_EXIT_PARTIAL
    SM.VI_UPPER_EXIT_PARTIAL = partial
    try:
        st = build()
        pos = put(st, buy=10_000, qty=100)
        st._opening_prices["X"] = 10_000
        # 정적 VI 상단 = 시가 x1.10 = 11,000 -> 그 바로 아래로 밀어넣는다
        px = int(11_000 * (1 - SM.VI_UPPER_MARGIN_PCT / 2))
        st.on_price_update("X", px)
        return st, pos
    finally:
        SM.VI_UPPER_EXIT_PARTIAL = _s


st, pos = vi_case(True)
so = sells_of(st)
check("VI 상단에서 매도가 나간다", len(so) == 1, f"매도 {len(so)}건")
if so:
    check("🔴 전량이 아니라 절반만 판다", so[0]["qty"] == 50, f"{so[0]['qty']}주")
    check("  잔량이 살아 있다", "X" in st.holdings,
          f"보유 {st.holdings.get('X', {}).get('qty')}주")
    check("  트레일 기준 고점이 세팅된다", float(pos.get("trail_peak") or 0) > 0,
          str(pos.get("trail_peak")))

st2, _ = vi_case(False)
so2 = sells_of(st2)
check("롤백(False): 종전대로 전량 매도", bool(so2) and so2[0]["qty"] == 100 and "X" not in st2.holdings,
      f"{so2[0]['qty'] if so2 else 0}주")


# ══════════════════════════════════════════════════════════
section("[4] 분할 잔량 보호 — 동적캡 재발동으로 털지 않는다")
# ══════════════════════════════════════════════════════════
check("PARTIAL_EXIT_REMAINDER_HOLD is True", SM.PARTIAL_EXIT_REMAINDER_HOLD is True)
src4 = inspect.getsource(SM.StrategyManager)
check("동적캡 경로가 잔량 보호 상수를 본다",
      "PARTIAL_EXIT_REMAINDER_HOLD" in src4)

# 이미 분할 1차가 나간 포지션에 동적캡이 다시 발동해도 잔량을 팔면 안 된다
s = build()
p = put(s, buy=10_000, qty=100)
p["partial_exited"] = True
p["qty"] = 50
p["tp_cap_upgraded"] = True
before = len(sells_of(s))
try:
    s.check_timeouts()
except Exception:
    pass
check("🔴 분할 후 잔량은 동적캡으로 청산되지 않는다",
      len(sells_of(s)) == before and "X" in s.holdings,
      f"매도 {len(sells_of(s)) - before}건 / 보유 {'O' if 'X' in s.holdings else 'X'}")

# 잔량이라도 손절은 그대로 작동해야 한다(안전성)
s = build()
p = put(s, buy=10_000, qty=100)
p["partial_exited"] = True
p["qty"] = 50
s.on_price_update("X", int(10_000 * (1 + SM.STOP_LOSS_RATE)) - 1)
check("🔴 잔량도 손절은 정상 발동", len(sells_of(s)) == 1, f"매도 {len(sells_of(s))}건")


# ══════════════════════════════════════════════════════════
section("[5] 편입가 앵커 — 되돌림 2차 트랜치")
# ══════════════════════════════════════════════════════════
check("ENTRY_ANCHOR_SECOND_TRANCHE is True", SM.ENTRY_ANCHOR_SECOND_TRANCHE is True)

TRIG = 10_000
cur_t2 = TRIG * (1 - SM.ENTRY_PULLBACK_TRANCHES[-1][0])     # 현행 2차 = 9,930


def plan_with(anchor):
    st = build()
    st._cond_names["X"] = "주도주상위"
    st._prev_closes["X"] = TRIG * 0.9
    st._opening_prices["X"] = TRIG
    st._first_seen["X"] = _t.time() - 60
    if anchor is not None:
        st._cond_hit_prices["X"] = anchor
    st._open_entry_plan("X", "X", "1A", {"current_price": TRIG,
                                         "entry_mode": "tick_driven"},
                        "1A", "주도주상위", TRIG)
    pl = st._entry_plans.get("X")
    return (pl["targets"][-1]["price"] if pl else None)


t_low = plan_with(cur_t2 - 50)      # 편입가가 현행 2차보다 낮다 -> 적용
t_high = plan_with(cur_t2 + 50)     # 편입가가 더 높다 -> 현행 유지 (downside 0)
t_none = plan_with(None)            # 편입가를 모른다 -> 현행 유지

check("편입가가 현행 2차보다 낮으면 그 값을 쓴다",
      t_low is not None and abs(t_low - (cur_t2 - 50)) < 1, f"{t_low}")
check("🔴 편입가가 더 높으면 현행 유지 (평단이 나빠지지 않는다)",
      t_high is not None and abs(t_high - cur_t2) < 1, f"{t_high} vs 현행 {cur_t2}")
check("편입가를 모르면 현행 유지('모름'이 매수를 막지 않는다)",
      t_none is not None and abs(t_none - cur_t2) < 1, f"{t_none}")
check("🔴 2차 가격은 언제나 현행보다 같거나 낮다(불변식)",
      all(v is not None and v <= cur_t2 + 1e-6 for v in (t_low, t_high, t_none)))

# 1차는 손대지 않는다 — 기회 손실 0의 근거
st = build()
st._cond_names["X"] = "주도주상위"
st._prev_closes["X"] = TRIG * 0.9
st._opening_prices["X"] = TRIG
st._first_seen["X"] = _t.time() - 60
st._cond_hit_prices["X"] = cur_t2 - 500
st._open_entry_plan("X", "X", "1A", {"current_price": TRIG,
                                     "entry_mode": "tick_driven"},
                    "1A", "주도주상위", TRIG)
pl = st._entry_plans.get("X")
if pl:
    t1 = pl["targets"][0]["price"]
    check("🔴 1차 트랜치는 현행 그대로 (기회 손실 0)",
          abs(t1 - TRIG * (1 - SM.ENTRY_PULLBACK_TRANCHES[0][0])) < 1, f"{t1:,.0f}")

# 롤백
_sv = SM.ENTRY_ANCHOR_SECOND_TRANCHE
SM.ENTRY_ANCHOR_SECOND_TRANCHE = False
try:
    check("롤백(False): 편입가가 낮아도 현행 2차를 쓴다",
          abs(plan_with(cur_t2 - 500) - cur_t2) < 1)
finally:
    SM.ENTRY_ANCHOR_SECOND_TRANCHE = _sv


# ══════════════════════════════════════════════════════════
section("[6] 불변식 — 경로 수가 늘지 않았는가")
# ══════════════════════════════════════════════════════════
buy_callers = [n for n in dir(SM.StrategyManager)
               if not n.startswith("__") and n != "_execute_buy"
               and callable(getattr(SM.StrategyManager, n, None))
               and "_execute_buy(" in (inspect.getsource(getattr(SM.StrategyManager, n))
                                       if inspect.isfunction(getattr(SM.StrategyManager, n))
                                       else "")]
check("_execute_buy 호출부가 여전히 5곳(함수 기준)", len(buy_callers) == 5,
      f"{len(buy_callers)}곳 {sorted(buy_callers)}")
check("신규매수 하드컷오프 유지", SM.ENTRY_HARD_CUTOFF == SM.time(15, 10))
check("시간 기반 자동청산은 여전히 전멸",
      OM.FORCE_CLOSE_ENABLED is False
      and SM.INDEX_GUARD_FORCE_CLOSE_ENABLED is False
      and SM.STAGNANT_EXIT_ENABLED is False)

print("\n" + "=" * 68)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
for f in FAIL:
    print("   FAIL:", f)
print("=" * 68)
sys.exit(1 if FAIL else 0)
