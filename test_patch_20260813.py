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

# ── 🔴 (2026-08-12 결함수정) 진단 분류가 숙성과 분리돼 있는가 ──────────
# 라벨이 "기타"로 뭉개지면 장중 체크리스트 N1("유효창 만료가 하루 2~3건인가")을
# **원리적으로 판정할 수 없다.** 사유 문자열만 다르고 분류가 같으면 소용없다.
check("🔴 유효창 만료가 '기타'로 뭉개지지 않는다",
      SM.StrategyManager._reject_category("진입 유효창 만료 (편입 후 45분 > 30분)")
      not in ("기타", SM.StrategyManager._reject_category("진입 숙성 미달 (편입 후 1초 < 30초)")),
      SM.StrategyManager._reject_category("진입 유효창 만료 (편입 후 45분 > 30분)"))
check("  숙성 분류는 그대로",
      SM.StrategyManager._reject_category("진입 숙성 미달 (편입 후 1초 < 30초)")
      == "진입 숙성 대기")

# ── 🔴 (2026-08-12 결함수정) 상한은 '새 포지션을 여는 매수'에만 건다 ────
# 그대로 두면 ① 되돌림 2차 트랜치가 막혀 **반쪽 포지션**이 되고
#            ② rescue-add가 편입 30분 뒤 **원리적으로 발동 불가**가 된다.
s4 = build()
s4._first_seen["Z"] = _t.time() - (SM.MAX_ENTRY_AGE_SEC + 60)
check("🔴 initiating=True(신규 진입)면 상한 적용",
      s4._entry_delay_reject("Z", initiating=True) is not None)
check("🔴 initiating=False(추가매수/트랜치)면 상한 미적용",
      s4._entry_delay_reject("Z", initiating=False) is None,
      str(s4._entry_delay_reject("Z", initiating=False)))
s4._first_seen["Z"] = _t.time() - 1     # 숙성 미달
check("  단, 숙성(하한)은 initiating과 무관하게 유지",
      s4._entry_delay_reject("Z", initiating=False) is not None)

# 🔴 숙성을 롤백해도 유효창이 같이 죽지 않아야 한다.
#    (결함: 검사가 `need <= 0`의 조기 return 뒤에 있어 함께 무효화됐다.
#     롤백표는 두 항목을 **별개**로 적고 있으므로 문서와 코드가 어긋난다.)
_a, _b = SM.MIN_ENTRY_DELAY_SEC, SM.MIN_ENTRY_DELAY_SEC_BY_COND
SM.MIN_ENTRY_DELAY_SEC, SM.MIN_ENTRY_DELAY_SEC_BY_COND = 0.0, {}
try:
    s5 = build()
    s5._first_seen["Z"] = _t.time() - (SM.MAX_ENTRY_AGE_SEC + 60)
    check("🔴 숙성 롤백(0초)해도 유효창 상한은 살아 있다",
          s5._entry_delay_reject("Z") is not None,
          str(s5._entry_delay_reject("Z")))
finally:
    SM.MIN_ENTRY_DELAY_SEC, SM.MIN_ENTRY_DELAY_SEC_BY_COND = _a, _b

# 실동작 — 29분 50초에 계획을 열고 2차가 30분 넘어 도달해도 2/2 체결
s6 = build()
s6._cond_names["T2"] = "주도주상위"
s6._prev_closes["T2"] = 9_000
s6._opening_prices["T2"] = 10_000
s6._first_seen["T2"] = _t.time() - (SM.MAX_ENTRY_AGE_SEC - 10)
s6._open_entry_plan("T2", "T2", "1A",
                    {"current_price": 10_000, "entry_mode": "tick_driven"},
                    "1A", "주도주상위", 10_000)
_pl = s6._entry_plans.get("T2")
check("계획이 열린다(유효창 안)", _pl is not None)
if _pl:
    _t1, _t2 = [t["price"] for t in _pl["targets"]]
    s6._try_fill_entry_plan("T2", _t1)
    s6._first_seen["T2"] = _t.time() - (SM.MAX_ENTRY_AGE_SEC + 60)   # 30분 경과
    s6._try_fill_entry_plan("T2", _t2)
    _bq = [o["qty"] for o in s6.order_manager.orders if o["side"] == "buy"]
    check("🔴 2차 트랜치가 유효창 만료로 막히지 않는다(반쪽 포지션 방지)",
          len(_bq) == 2, f"트랜치 {_bq}")

# 실동작 — rescue-add가 편입 40분 뒤에도 집행된다
def _rescue_at(age_sec):
    st = build()
    st.holdings["R9"] = {
        "trade_id": 1, "buy_price": 10_000, "buy_quantity": 100, "qty": 100,
        "buy_time": st._now() - timedelta(minutes=40), "stock_name": "R9",
        "strategy_phase": 1, "sub_strategy": "1A", "highest_price": 10_000,
        "lowest_price": 9_700, "warmup_until": st._now() - timedelta(seconds=1),
        "origin_price": 10_000, "stop_rate": None, "tranches_filled": 2}
    st._prev_closes["R9"] = 9_000
    st._opening_prices["R9"] = 10_000
    st._cond_names["R9"] = "주도주상위"
    st._first_seen["R9"] = _t.time() - age_sec
    st._volume_ratio = lambda c: 3.0
    ok = st._do_rescue_add("R9", st.holdings["R9"], 9_550, 0.0, "테스트")
    return ok, len([o for o in st.order_manager.orders if o["side"] == "buy"])

_ok_e, _n_e = _rescue_at(600)
_ok_l, _n_l = _rescue_at(SM.MAX_ENTRY_AGE_SEC + 600)
check("rescue-add: 편입 10분 뒤 정상 집행 (대조군)", _ok_e is True and _n_e == 1,
      f"{_ok_e}/{_n_e}건")
check("🔴 rescue-add: 편입 40분 뒤에도 집행된다(유효창이 죽이지 않는다)",
      _ok_l is True and _n_l == 1, f"{_ok_l}/{_n_l}건")


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

# ── 🔴 (2026-08-12 결함수정) 연속 틱 — 자기 잔량을 다시 털지 않는가 ──────
# 위 단발 틱 검사만으로는 결함이 안 잡혔다. VI 상단 밴드는 0.5%/2호가라
# 상승 중 종목은 그 안에 **여러 틱** 머무는데, 분할이 나가면 partial_exited가
# True가 되어 **바로 다음 틱**에 전량 매도 분기로 떨어졌다(실측 재현).
# 그러면 '50% 분할'이 사실상 1틱짜리 무효 기능이 된다.
# ⚠️ 매수가를 시가보다 위(+7%)로 둬야 실전형이다 — 시가에 사면 VI 도달 시
#    순 +9.5%라 익절캡(6%)이 먼저 잡아 이 결함이 가려진다.
def vi_multi(n_ticks, price=10_970, buy=10_700, open_px=10_000):
    st = build()
    p = put(st, buy=buy, qty=100)
    st._opening_prices["X"] = open_px
    for _ in range(n_ticks):
        if "X" in st.holdings:
            st.on_price_update("X", price)
    return st, p


stm, pm = vi_multi(4)
check("🔴 같은 가격 4틱에도 잔량이 살아남는다(분할이 무효가 아니다)",
      "X" in stm.holdings and stm.holdings["X"]["qty"] == 50,
      f"보유 {stm.holdings.get('X', {}).get('qty', '청산됨')}")
check("  매도는 1회(50주)만 나갔다", [o["qty"] for o in sells_of(stm)] == [50],
      str([o["qty"] for o in sells_of(stm)]))
check("  vi_partial_done 표식이 찍힌다",
      stm.holdings.get("X", {}).get("vi_partial_done") is True)

# 잔량은 무방비가 아니다 — 트레일/손절이 그대로 받는다.
stt, _ = vi_multi(2)
if "X" in stt.holdings:
    stt.on_price_update("X", int(10_970 * (1 - SM.PARTIAL_EXIT_TRAIL) - 1))
check("🔴 잔량은 트레일로 청산된다(방치되지 않는다)", "X" not in stt.holdings,
      f"보유 {stt.holdings.get('X', {}).get('qty', '청산됨')}")

sts, _ = vi_multi(2)
if "X" in sts.holdings:
    sts.on_price_update("X", int(10_700 * (1 + SM.STOP_LOSS_RATE)) - 1)
check("🔴 잔량도 손절은 정상 발동", "X" not in sts.holdings,
      f"보유 {sts.holdings.get('X', {}).get('qty', '청산됨')}")

# 의도된 동작 보존 — **다른 규칙**이 이미 분할한 포지션은 VI에서 전량 정리한다
sto = build()
po = put(sto, buy=10_700, qty=100)
sto._opening_prices["X"] = 10_000
po["partial_exited"] = True          # 동적캡/반등소진이 이미 절반 뺀 상태
po["qty"] = 50
sto.on_price_update("X", 10_970)
check("의도 보존: 다른 규칙의 분할 잔량은 VI에서 전량 정리",
      "X" not in sto.holdings and [o["qty"] for o in sells_of(sto)] == [50],
      f"{[o['qty'] for o in sells_of(sto)]}")

# 롤백해도 연속 틱에서 동작이 종전과 같아야 한다(전량 1회)
_svp = SM.VI_UPPER_EXIT_PARTIAL
SM.VI_UPPER_EXIT_PARTIAL = False
try:
    str_, _ = vi_multi(4)
    check("롤백(False): 연속 틱에서도 전량 1회",
          "X" not in str_.holdings and [o["qty"] for o in sells_of(str_)] == [100],
          str([o["qty"] for o in sells_of(str_)]))
finally:
    SM.VI_UPPER_EXIT_PARTIAL = _svp


# ══════════════════════════════════════════════════════════
section("[4] 분할 잔량 보호 — 동적캡 재발동으로 털지 않는다")
# ══════════════════════════════════════════════════════════
check("PARTIAL_EXIT_REMAINDER_HOLD is True", SM.PARTIAL_EXIT_REMAINDER_HOLD is True)
src4 = inspect.getsource(SM.StrategyManager)
check("동적캡 경로가 잔량 보호 상수를 본다",
      "PARTIAL_EXIT_REMAINDER_HOLD" in src4)

# 🔴 (2026-08-12 재작성) 예전 검사는 **공허했다**.
#    틱·거래량을 하나도 주지 않고 check_timeouts()만 불렀는데, 그러면
#    `_update_dynamic_caps`가 강도 중립값에서 곧바로 continue 해버려
#    **동적캡 경로에 도달조차 하지 않는다.** 즉 상수를 False로 되돌려도
#    똑같이 통과했다(A/B 실측 동일). 이 코드베이스가 반복 경고해 온
#    '검증이 거짓말한다'의 전형이라, 실제로 발동하는 시나리오로 바꾸고
#    **A/B가 갈리는지**를 단언한다.
def dyncap_case(hold_remainder, already_partial):
    _sv = SM.PARTIAL_EXIT_REMAINDER_HOLD
    SM.PARTIAL_EXIT_REMAINDER_HOLD = hold_remainder
    try:
        st = build()
        p = put(st, buy=10_000, qty=100)
        p["tp_cap_upgraded"] = True
        p["tp_cap"] = SM.TP_CAP_UPGRADED_MAX
        p["strength_baseline"] = 200.0
        p["tranches_filled"] = 2
        if already_partial:
            p["partial_exited"] = True
            p["qty"] = 50
        # 강도 하락(매도 우위) + 거래량 하락 + 순이익 구간을 실제로 만든다
        _now = _t.time()
        for i in range(12):
            st.phase1b.trade_flow.add_tick("X", 10_300, "sell", 50, now=_now - i * 0.2)
            st.phase1b.trade_flow.add_tick("X", 10_300, "buy", 1, now=_now - i * 0.2)
        # 이 스위트의 _Rest는 분봉을 빈 리스트로 돌려준다 -> candles가 비면
        # vol_ratio가 None이 되어 **동적캡 경로에 도달하지 못한다**(옛 검사가
        # 공허했던 진짜 원인). 거래량 하락 상황을 명시적으로 만든다.
        st._get_merged_candles = lambda *a, **kw: [
            {"close": 10_300, "volume": 100} for _ in range(30)
        ]
        st._volume_ratio = lambda c: 0.3
        st._update_dynamic_caps()
        return st
    finally:
        SM.PARTIAL_EXIT_REMAINDER_HOLD = _sv


# 대조군 — 이 시나리오에서 동적캡이 **실제로** 발동하는가(공허하지 않은가)
ctl = dyncap_case(True, already_partial=False)
check("🔴 [대조군] 동적캡이 실제로 발동하는 시나리오다",
      [o["qty"] for o in sells_of(ctl)] == [50],
      f"매도 {[o['qty'] for o in sells_of(ctl)]}")

on = dyncap_case(True, already_partial=True)
off = dyncap_case(False, already_partial=True)
check("🔴 분할 후 잔량은 동적캡으로 청산되지 않는다",
      not sells_of(on) and "X" in on.holdings,
      f"매도 {len(sells_of(on))}건 / 보유 {on.holdings.get('X', {}).get('qty', '청산됨')}")
check("🔴 [A/B] 롤백(False)에서는 잔량이 실제로 털린다 — 검사가 공허하지 않다",
      bool(sells_of(off)) and "X" not in off.holdings,
      f"매도 {[o['qty'] for o in sells_of(off)]} / 보유 "
      f"{off.holdings.get('X', {}).get('qty', '청산됨')}")

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
# 🔴 08-12 저녁에 **OFF로 되돌렸다** — 시뮬레이션 결함(2차 미체결 시 1차만
#    체결되어 평단이 오르는 효과를 빠뜨림) + 실측에서 구간마다 방향이 뒤집힘.
#    끈 기능이라도 **배선 테스트는 남긴다**(되살릴 때 검증이 없으면 사고가 난다).
check("🔴 ENTRY_ANCHOR_SECOND_TRANCHE 기본 OFF",
      SM.ENTRY_ANCHOR_SECOND_TRANCHE is False)

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


# OFF 상태(기본)에서는 편입가가 아무리 낮아도 현행 2차를 쓴다.
check("🔴 OFF: 편입가가 낮아도 현행 2차를 쓴다",
      abs(plan_with(cur_t2 - 500) - cur_t2) < 1, f"{plan_with(cur_t2 - 500)}")

# ── 켰을 때의 배선 (되살릴 때를 위해 남겨 둔다) ──────────────────────
_sv_anchor = SM.ENTRY_ANCHOR_SECOND_TRANCHE
SM.ENTRY_ANCHOR_SECOND_TRANCHE = True
try:
    t_low = plan_with(cur_t2 - 50)      # 편입가가 현행 2차보다 낮다 -> 적용
    t_high = plan_with(cur_t2 + 50)     # 편입가가 더 높다 -> 현행 유지
    t_none = plan_with(None)            # 편입가를 모른다 -> 현행 유지
    check("ON: 편입가가 현행 2차보다 낮으면 그 값을 쓴다",
          t_low is not None and abs(t_low - (cur_t2 - 50)) < 1, f"{t_low}")
    check("ON: 편입가가 더 높으면 현행 유지",
          t_high is not None and abs(t_high - cur_t2) < 1, f"{t_high} vs {cur_t2}")
    check("ON: 편입가를 모르면 현행 유지('모름'이 매수를 막지 않는다)",
          t_none is not None and abs(t_none - cur_t2) < 1, f"{t_none}")
    check("ON: 2차 **가격**은 언제나 현행 이하 (단, 평단은 별개 — 아래 주석)",
          all(v is not None and v <= cur_t2 + 1e-6 for v in (t_low, t_high, t_none)))
    # 🔴 이 불변식이 'downside 0'을 뜻하지 않는다는 것이 08-12 저녁의 교훈이다.
    #    2차가 깊어지면 **미체결 -> 1차만 체결 -> 평단 상승**이 일어난다.
    #    실측: 09:00~09:10 트랜치 13->11개, 평단 +0.008%(남화토건 +12.5원).
finally:
    SM.ENTRY_ANCHOR_SECOND_TRANCHE = _sv_anchor

# 1차는 어느 설정에서도 손대지 않는다
_sv_anchor = SM.ENTRY_ANCHOR_SECOND_TRANCHE
SM.ENTRY_ANCHOR_SECOND_TRANCHE = True
try:
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
        check("ON이어도 1차 트랜치는 현행 그대로",
              abs(t1 - TRIG * (1 - SM.ENTRY_PULLBACK_TRANCHES[0][0])) < 1,
              f"{t1:,.0f}")
finally:
    SM.ENTRY_ANCHOR_SECOND_TRANCHE = _sv_anchor

# 앵커 캐시는 OFF여도 계속 쌓인다 — 재검증 재료를 잃지 않기 위해.
_stc = build()
_stc.prearm_candidate("Q", 1234.0)
check("OFF여도 편입가 캐시는 남는다(재검증 재료 보존)",
      abs(_stc._cond_hit_prices.get("Q", 0) - 1234.0) < 1e-6,
      str(_stc._cond_hit_prices.get("Q")))


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
