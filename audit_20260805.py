# -*- coding: utf-8 -*-
"""08-05 전면 감사 — 오늘 변경분이 서로 충돌하거나 매수/매도를 잘못 내지 않는가.

기존 8개 스위트가 '각 기능이 스펙대로 도는가'를 본다면, 이 파일은
**"변경 A가 변경 B의 사양을 무력화하지 않는가"**와 **"어떤 경로로도 잘못된
매수/매도가 나가지 않는가"**만 따로 훑는다.

08-05 변경 목록(이 감사의 대상):
  ① 버스트 주가 스케일 (ALPHA 0.55 / MIN 0.30 / MAX 2.00)
  ② 상대 경로 하한 = burst_min (저가주 뒷문 차단)
  ③ 재매수 배수 2.0 -> 2.5
  ④ 매수금액 50만 -> 200만 (3곳)
  ⑤ 정적VI 상단 근접 확정매도
  ⑥ 종가베팅 후보 0건 알림
  ⑦ 일일 백테스트 리포트 헤더

실행: PYTHONIOENCODING=utf-8 AUTOTRADER_TEST_LOG=1 python audit_20260805.py
"""
import os
import sys

os.environ.setdefault("AUTOTRADER_TEST_LOG", "1")   # 반드시 core/main 임포트보다 먼저

import ast
import glob
import inspect
import time as _t
from datetime import datetime, timedelta, time as dtime

PASS, FAIL = [], []
T0 = _t.time()


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


def section(t):
    print("\n" + "=" * 66)
    print(t)
    print("=" * 66)


# ──────────────────────────────────────────────────────────────
section("[1] 임포트 · 구문 · 배선")
# ──────────────────────────────────────────────────────────────
import main                                     # noqa: E402
import core.strategy_manager as SM              # noqa: E402
import core.daily_backtest as DB                # noqa: E402
import core.slot_replacement as SR              # noqa: E402
from core.strategy.portfolio_optimizer import DEFAULT_BASE_AMOUNT   # noqa: E402
from core.order_manager import BUY_AMOUNT_PER_STOCK, FORCE_CLOSE_TIME  # noqa: E402
from utils.price_helper import add_ticks, get_tick_size             # noqa: E402

check("import main 성공", True)

bad = []
for f in glob.glob("**/*.py", recursive=True):
    if ".git" in f or "__pycache__" in f or ".claude" in f:
        continue
    try:
        ast.parse(open(f, encoding="utf-8").read())
    except SyntaxError as e:
        bad.append(f"{f}:{e.lineno}")
check(f"전체 .py 구문오류 0건", not bad, ", ".join(bad[:5]))

src_main = inspect.getsource(main)
defined = {n for n in dir(main.TradingBot) if n.startswith("task_")}
registered = {n for n in defined if f"self.{n}()" in src_main}
check("정의된 태스크 == gather 등록 태스크",
      defined == registered,
      f"정의 {len(defined)} / 등록 {len(registered)} / 누락 {sorted(defined - registered)}")

# 지연 import 심볼 실존 (봇은 안 죽고 기능만 조용히 사라지는 부류)
missing = []
for mod, names in [
    ("core.daily_backtest", ["run_daily_backtest"]),
    ("core.slot_replacement", ["try_slot_replacement"]),
    ("core.explosion_scorer", ["evaluate_closing_bet_candidate"]),
    ("core.history_cache", ["fetch_with_cache", "slice_today", "cache_stats"]),
    ("core.history_fetcher", ["to_trade_value_bins"]),
    ("core.tick_archive", ["archive_universe"]),
]:
    m = __import__(mod, fromlist=["*"])
    for n in names:
        if not hasattr(m, n):
            missing.append(f"{mod}.{n}")
check("지연 import 심볼 전수 실존", not missing, ", ".join(missing))


# ──────────────────────────────────────────────────────────────
section("[2] 오늘 바뀐 수치 — 정확성")
# ──────────────────────────────────────────────────────────────
check("매수금액 3곳 일치 = 200만원",
      SM.POSITION_AMOUNT == DEFAULT_BASE_AMOUNT == BUY_AMOUNT_PER_STOCK == 2_000_000,
      f"{SM.POSITION_AMOUNT:,} / {DEFAULT_BASE_AMOUNT:,} / {BUY_AMOUNT_PER_STOCK:,}")
check("버스트 주가 스케일 상수",
      (SM.BURST_PRICE_REF, SM.BURST_PRICE_ALPHA,
       SM.BURST_PRICE_MIN, SM.BURST_PRICE_MAX) == (10_000.0, 0.55, 0.30, 2.00),
      f"{SM.BURST_PRICE_REF}/{SM.BURST_PRICE_ALPHA}/{SM.BURST_PRICE_MIN}/{SM.BURST_PRICE_MAX}")
check("재매수 배수 2.5", SM.REBUY_BURST_VALUE_MULT == 2.5)
check("VI 상수", (SM.VI_UPPER_EXIT_ENABLED, SM.VI_STATIC_RATIO,
                 SM.VI_UPPER_MARGIN_PCT, SM.VI_UPPER_MARGIN_TICKS)
      == (True, 0.10, 0.005, 2))

# 상한 클램프가 걸리는 주가 (오해 방지용 — MAX만 보면 안 된다)
bind = 10_000 * SM.BURST_PRICE_MAX ** (1 / SM.BURST_PRICE_ALPHA)
check("상한 클램프 시작 주가 35,264원 부근", 35_000 <= bind <= 35_500, f"{bind:,.0f}원")

# 실제 문턱표 — 수치를 눈으로 확인할 수 있게 출력
print("\n  주가별 실효 문턱:")
for px in (1_000, 2_000, 5_000, 10_000, 20_000, 35_264, 50_000, 150_000):
    m = SM.burst_price_scale(px)
    print(f"    {px:>8,}원  x{m:.3f}  절대 {SM.PHASE1A_BURST_TRADE_VALUE*m/10000:>7,.0f}만"
          f"  단일 {SM.PHASE1A_SINGLE_TRADE_VALUE*m/1e8:>5.2f}억"
          f"  재매수 {SM.PHASE1A_BURST_TRADE_VALUE*m*SM.REBUY_BURST_VALUE_MULT/10000:>7,.0f}만"
          f"  VI상단 {px*(1+SM.VI_STATIC_RATIO):>9,.0f}원")

check("스케일 단조증가", all(SM.burst_price_scale(a) <= SM.burst_price_scale(b)
                        for a, b in zip([1_000, 5_000, 20_000, 60_000],
                                        [5_000, 20_000, 60_000, 200_000])))
check("모든 주가대에서 절대문턱 > 0", all(SM.PHASE1A_BURST_TRADE_VALUE
                                  * SM.burst_price_scale(p) > 0
                                  for p in range(500, 200_000, 2_500)))
check("재매수 문턱은 항상 일반보다 크다", SM.REBUY_BURST_VALUE_MULT > 1.0)


# ──────────────────────────────────────────────────────────────
section("[3] 상수 불변식 — 서로 모순되지 않는가")
# ──────────────────────────────────────────────────────────────
check("진입 종료 <= 강제청산", SM.PHASE1A_END <= dtime(*map(int, FORCE_CLOSE_TIME.split(":"))))
check("1A/Pullback 진입 종료 동일", SM.PHASE1A_END == SM.PULLBACK_END)
check("슬롯 캡 <= 하드 상한", SM.PHASE1A_MAX_SLOTS <= SM.MAX_HOLDINGS_HARD
      and SM.PULLBACK_MAX_SLOTS <= SM.MAX_HOLDINGS_HARD)
check("MAX_HOLDINGS <= HARD", SM.MAX_HOLDINGS <= SM.MAX_HOLDINGS_HARD)
check("무장 시간 < 무장 TTL", SM.TICK_STRENGTH_SUSTAIN_SEC < SM.TICK_ARM_TTL_SEC)
check("시간계수 전부 <= 1.0", all(v <= 1.0 for _, v in SM.TICK_BURST_TIME_MULT))
check("모든 익절캡 > 왕복수수료",
      min(SM.TAKE_PROFIT_CAP, SM.TAKE_PROFIT_CAP_PULLBACK,
          SM.TAKE_PROFIT_CAP_EARLY) > SM.ROUND_TRIP_COST)
check("기본캡 != 상향캡 (08-03 결함① 재발 방지)",
      SM.TAKE_PROFIT_CAP != SM.TP_CAP_UPGRADED_MAX)
check("지수가드 임계 == SEVERE_CRASH (한쪽만 바뀌면 어긋남)",
      SM.INDEX_GUARD_THRESHOLD == SM.SEVERE_CRASH_THRESHOLD
      if hasattr(SM, "SEVERE_CRASH_THRESHOLD") else True)
check("본전바닥 > 0 (수수료 슬리피지 방어)", SM.BREAKEVEN_FLOOR > 0)
check("되돌림 트랜치 비중 합 = 1.0",
      abs(sum(w for _, w in SM.ENTRY_PULLBACK_TRANCHES) - 1.0) < 1e-9)
check("되돌림 깊이 오름차순",
      all(a[0] < b[0] for a, b in zip(SM.ENTRY_PULLBACK_TRANCHES,
                                      SM.ENTRY_PULLBACK_TRANCHES[1:])))
check("VI 마진 < 정적VI 폭 (밴드가 뒤집히지 않음)",
      SM.VI_UPPER_MARGIN_PCT < SM.VI_STATIC_RATIO)
check("추가매수 관찰 하한 > 손절선 (구조 전 더 내려갈 여지)",
      SM.RESCUE_ADD_OBSERVE_FLOOR > abs(SM.STOP_LOSS_RATE))
check("추가매수 최종손절 > 관찰 하한", SM.RESCUE_ADD_FINAL_STOP > SM.RESCUE_ADD_OBSERVE_FLOOR)


# ──────────────────────────────────────────────────────────────
section("[4] 🔴 잘못된 매도가 나가지 않는가 (전 경로)")
# ──────────────────────────────────────────────────────────────
# ⚠️ test_patch_20260805를 import하면 그 파일이 통째로 실행되고 마지막
#    sys.exit()에서 이 감사가 끊긴다. 그래서 헬퍼를 여기 자체 정의한다.
from core.phase1b_controller import Phase1BController   # noqa: E402


class _Repo:
    rows, sells, updates = [], [], []

    @classmethod
    def find_holdings(cls):
        return []

    @classmethod
    def find_by_date(cls, d):
        return []

    @classmethod
    def insert_buy(cls, **kw):
        cls.rows.append(kw)
        return len(cls.rows)

    @classmethod
    def update_sell(cls, **kw):
        cls.sells.append(kw)
        return True

    @classmethod
    def update(cls, i, d):
        cls.updates.append({"id": i, **d})
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


class _Theme:
    def __init__(self, *a, **kw):
        self.code_to_theme = {}
        self.leading_themes = []

    def fetch_themes_from_github(self): pass

    def start_auto_update(self, *a, **kw): pass

    def is_leading_theme_stock(self, c): return False


class _ThemeOld:
    def __init__(self, *a, **kw):
        pass

    def __getattr__(self, n):
        return lambda *a, **kw: None


class _Rest:
    """⚠️ 반환 형식은 test_patch_20260805.py의 검증된 스텁과 **반드시 동일**해야
    한다. 처음엔 __getattr__로 전부 None을 돌려주게 만들었다가, 매도가 하나도
    안 나가는 가짜 통과/실패를 만들었다(get_index_change_rate가 None -> min()
    TypeError). 스텁이 실물과 다르면 감사 자체가 거짓말을 한다."""
    host = "https://api.kiwoom.com"

    def get_minute_candles(self, code, interval=1, count=1, base_date=None):
        return [{"time_str": "20260805090000", "open": 9_990, "high": 10_010,
                 "low": 9_980, "close": 10_000, "volume": 1000}] * max(count, 20)

    def get_orderable_amount(self):
        return 10_835_694

    def get_stock_change_rate(self, code):
        return 3.0

    def get_index_change_rate(self, s="001"):
        return 0.0

    def get_current_price(self, code):
        return 10_000


class _OrderMgr:
    def __init__(self):
        self.orders = []

    def buy(self, code, qty, price=0, sizing="REGULAR", exit_strategy="REGULAR",
            order_style="limit", ref_price=0):
        self.orders.append({"code": code, "qty": qty, "side": "buy"})
        return {"success": True, "ord_no": "1", "price": ref_price or 10_000,
                "style": order_style}

    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"code": code, "qty": qty, "side": "sell"})
        return {"success": True, "ord_no": "2", "price": price or 10_000,
                "style": order_style}

    def get_stock_name(self, code):
        return code


class _Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


def build(now_dt=datetime(2026, 8, 5, 10, 0, 0)):
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows, _Repo.sells, _Repo.updates = [], [], []
    return SM.StrategyManager(kiwoom_rest=_Rest(), order_manager=_OrderMgr(),
                              phase1b_controller=Phase1BController(),
                              portfolio_optimizer=None,
                              now_func=_Clock(now_dt)), _Clock(now_dt)


def put_pos(s, code="R1", buy=10_000, qty=100, warm=False):
    s.holdings[code] = {
        "trade_id": 1, "buy_price": buy, "origin_price": buy,
        "buy_quantity": qty, "qty": qty, "buy_time": s._now(),
        "stock_name": code, "strategy_phase": "1A", "sub_strategy": "1A",
        "highest_price": buy, "lowest_price": buy, "ma20": None,
        "ma20_updated": None,
        "warmup_until": s._now() + timedelta(seconds=(999 if warm else -1)),
    }
    return s.holdings[code]


def sells_of(fn):
    _Repo.sells = []
    fn()
    return [x.get("exit_reason") or "" for x in _Repo.sells]


def pos_at(price, buy=10_000, open_px=None, warm=False, now=None):
    s, clk = build(now or datetime(2026, 8, 5, 10, 0, 0))
    put_pos(s, "Z", buy=buy, warm=warm)
    if open_px:
        s._opening_prices["Z"] = open_px
    return s


# 매수 직후(워밍업 중) 가격이 거의 안 움직였는데 파는 일이 없어야 한다
r = sells_of(lambda: pos_at(10_000, warm=True).on_price_update("Z", 10_010))
check("🔴 워밍업 중 미미한 변동으로 매도하지 않음", not r, str(r))
r = sells_of(lambda: pos_at(10_000, warm=False).on_price_update("Z", 10_010))
check("🔴 워밍업 종료 후에도 미미한 변동으로 매도하지 않음", not r, str(r))
# 소폭 이익 — 어떤 규칙도 걸리면 안 된다
r = sells_of(lambda: pos_at(10_000).on_price_update("Z", 10_100))
check("🔴 +1.0%에서 매도하지 않음(본전스톱 무장만)", not r, str(r))
# 시가 캐시가 없는데 VI가 발동하면 안 된다
r = sells_of(lambda: pos_at(10_000).on_price_update("Z", 10_300))
check("🔴 시가 캐시 없으면 VI 매도 없음", not any("VI" in x for x in r), str(r))
# 손절선 바로 위
r = sells_of(lambda: pos_at(10_000).on_price_update("Z", 9_705))
check("🔴 손절선 직전(-2.95%)에서 손절하지 않음", not any("손절" in x for x in r), str(r))
# 손절선 도달 -> 반드시 나간다 (추가매수 조건 미충족 시)
r = sells_of(lambda: pos_at(10_000).on_price_update("Z", 9_700))
check("✅ 손절선(-3%) 도달 시 반드시 청산", any("손절" in x for x in r), str(r))
# 가격 위생검사 — 여기에 이상한 값이 들어오면 그 자리에서 시장가 매도가 나간다
s = pos_at(10_000)
crashed = []
for bad_px in (0, None, -1, "", "abc", -9_800, 1, 10_000_000, float("nan")):
    try:
        s.on_price_update("Z", bad_px)
    except Exception as e:
        crashed.append(f"{bad_px!r}: {e}")
check("🔴 이상 가격 9종에 예외 없음", not crashed, "; ".join(crashed)[:80])
check("🔴 이상 가격 9종 어디서도 매도가 나가지 않음", "Z" in s.holdings,
      str([x.get("exit_reason") for x in _Repo.sells])[:80])
# 특히 음수: 부호 파싱이 한 곳만 바뀌어도 -100%로 읽혀 즉시 손절이 나간다
_Repo.sells = []
s2 = pos_at(10_000)
s2.on_price_update("Z", -9_800)
check("🔴 음수 가격에 손절이 나가지 않음(부호 파싱 회귀 방지)",
      "Z" in s2.holdings and not _Repo.sells)
# 정상 범위는 그대로 동작해야 한다 (가드가 과잉차단하지 않는지 대조군)
_Repo.sells = []
s3 = pos_at(10_000)
s3.on_price_update("Z", 9_700)
check("✅ 대조군: 정상 가격이면 가드가 막지 않는다",
      any("손절" in (x.get("exit_reason") or "") for x in _Repo.sells))
# KRX 일일 상하한(±30%)은 반드시 통과해야 한다
for px, label in ((13_000, "+30% 상한가"), (7_000, "-30% 하한가")):
    _Repo.sells = []
    s4 = pos_at(10_000)
    s4.on_price_update("Z", px)
    check(f"✅ {label}는 정상 처리(가드에 걸리지 않음)", bool(_Repo.sells),
          str([x.get("exit_reason") for x in _Repo.sells])[:60])
# 보유하지 않은 종목
try:
    pos_at(10_000).on_price_update("없음", 10_000)
    check("🔴 미보유 종목 가격갱신은 무해", True)
except Exception as e:
    check("🔴 미보유 종목 가격갱신은 무해", False, str(e))


# ──────────────────────────────────────────────────────────────
section("[5] 🔴 잘못된 매수가 나가지 않는가")
# ──────────────────────────────────────────────────────────────
def burst_ok(price, each, n=2, code="B"):
    s, _ = build()
    s.phase1b.start_watching(code)
    tf = s.phase1b.trade_flow
    now = _t.time()
    for i in range(40):
        tf.add_tick(code, price, "buy", max(1, int(200_000 // price)), now=now - 110 + i)
    for i in range(n):
        tf.add_tick(code, price, "buy", int(each // price), now=now - 2 + i * 0.3)
    return s.check_burst(code, now=now)


ok, d = burst_ok(2_000, SM.PHASE1A_BURST_TRADE_VALUE * SM.burst_price_scale(2_000) * 0.9)
check("🔴 저가주도 스케일된 문턱 미달이면 매수 안 함", not ok, d.get("reason", "")[:60])
ok, d = burst_ok(30_000, SM.PHASE1A_BURST_TRADE_VALUE)
check("🔴 고가주는 옛 문턱(4천만)만으로 매수 안 함", not ok, d.get("reason", "")[:60])
ok, d = burst_ok(30_000, SM.PHASE1A_BURST_TRADE_VALUE * SM.burst_price_scale(30_000) * 1.1)
check("✅ 고가주도 스케일된 문턱 넘으면 매수", ok, str(d.get("burst_path")))
# 상대 경로가 절대보다 헐거워질 수 없다 (저가주 뒷문)
_, d = burst_ok(1_500, 5_000_000)
check("🔴 상대 하한 >= 절대문턱 (뒷문 차단)",
      d.get("rel_min", 0) >= d.get("burst_min", 0),
      f"rel {d.get('rel_min'):,.0f} / abs {d.get('burst_min'):,.0f}")
# 재매수는 항상 더 엄격
ok_n, _ = burst_ok(10_000, SM.PHASE1A_BURST_TRADE_VALUE * 1.05)
s2, _ = build()
s2.phase1b.start_watching("B2")
tf2 = s2.phase1b.trade_flow
nw = _t.time()
for i in range(40):
    tf2.add_tick("B2", 10_000, "buy", 20, now=nw - 110 + i)
for i in range(2):
    tf2.add_tick("B2", 10_000, "buy", int(SM.PHASE1A_BURST_TRADE_VALUE * 1.05 // 10_000),
                 now=nw - 2 + i * 0.3)
ok_r, _ = s2.check_burst("B2", now=nw, value_mult=SM.REBUY_BURST_VALUE_MULT,
                         allow_relative=False)
check("🔴 일반은 통과해도 재매수 기준엔 미달", ok_n and not ok_r, f"일반 {ok_n} / 재매수 {ok_r}")
# 틱이 아예 없으면 매수 안 함
s3, _ = build()
s3.phase1b.start_watching("B3")
ok3, d3 = s3.check_burst("B3")
check("🔴 체결틱이 없으면 매수 안 함", not ok3, str(d3.get("reason"))[:50])


# ──────────────────────────────────────────────────────────────
section("[6] 청산 우선순위 — 실제 실행으로 확인")
# ──────────────────────────────────────────────────────────────
def scenario(label, price, buy=10_000, open_px=None, guard=False,
             warm=False, now=datetime(2026, 8, 5, 11, 10, 0)):
    s, _ = build(now)
    put_pos(s, "P", buy=buy, warm=warm)
    if open_px:
        s._opening_prices["P"] = open_px
    if guard:
        s._is_index_guard_active = lambda now_dt=None: True
    _Repo.sells = []
    s.on_price_update("P", price)
    why = " | ".join(x.get("exit_reason") or "" for x in _Repo.sells) or "(보유유지)"
    print(f"    {label:<44} -> {why[:56]}")
    return why


w = scenario("손절 -3% + VI 근접 + 가드", 9_700, buy=10_000, open_px=8_900, guard=True)
check("손절이 최우선", "손절" in w, w[:50])
w = scenario("가드 + VI 근접 (이익)", 10_960, buy=10_000, open_px=10_000, guard=True)
check("가드가 VI보다 우선", "지수 가드" in w, w[:50])
w = scenario("VI 근접만 (이익, 캡 미달)", 11_840, buy=11_500, open_px=10_800)
check("VI가 캡보다 먼저 확정", "VI 상단" in w, w[:60])
w = scenario("일반 익절캡 도달", 10_450, buy=10_000)
check("VI 조건 없으면 익절캡", "익절" in w and "VI" not in w, w[:50])
w = scenario("워밍업 중 손절", 9_700, buy=10_000, warm=True)
check("워밍업 중에도 손절 작동", "손절" in w, w[:50])
w = scenario("워밍업 중 소폭 이익", 10_150, buy=10_000, warm=True)
check("워밍업 중 익절캡은 대기", w == "(보유유지)", w[:50])


# ──────────────────────────────────────────────────────────────
section("[7] daily_backtest ↔ 라이브 동기화")
# ──────────────────────────────────────────────────────────────
check("백테스트가 라이브 시간창 상수를 직접 참조",
      DB.PULLBACK_START_HHMM == SM.PULLBACK_START.strftime("%H%M")
      and DB.PULLBACK_END_HHMM == SM.PULLBACK_END.strftime("%H%M"),
      f"{DB.PULLBACK_START_HHMM}~{DB.PULLBACK_END_HHMM}")
check("백테스트가 resolve_strategy를 공유", "resolve_strategy" in inspect.getsource(DB._entry_signal))
check("리포트 헤더에 실체결 대조군이 있다",
      "_today_live_trade_count" in inspect.getsource(DB._format_report))
check("실체결 건수 조회가 DB 실패에도 안전",
      "except Exception" in inspect.getsource(DB._today_live_trade_count))
check("종가베팅 후보 0건도 텔레그램 발송",
      "종가베팅 후보 없음" in src_main)


# ──────────────────────────────────────────────────────────────
section("[8] 핫패스 성능 (틱당 비용)")
# ──────────────────────────────────────────────────────────────
s, _ = build()
s.phase1b.start_watching("PERF")
tf = s.phase1b.trade_flow
nw = _t.time()
for i in range(3000):
    tf.add_tick("PERF", 10_000, "buy", 10, now=nw - 300 + i * 0.1)
t1 = _t.time()
for _ in range(300):
    s.check_burst("PERF", now=nw)
el = (_t.time() - t1) / 300 * 1000
check(f"check_burst 1회 < 5ms", el < 5.0, f"{el:.3f}ms")

s2, _ = build()
put_pos(s2, "PERF2", buy=10_000)
s2._opening_prices["PERF2"] = 10_000
t1 = _t.time()
for i in range(2000):
    s2.on_price_update("PERF2", 10_100 + (i % 5))
el = (_t.time() - t1) / 2000 * 1e6
check(f"on_price_update 1회 < 200us (VI 추가 후)", el < 200, f"{el:.1f}us")


print("\n" + "=" * 66)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건   ({_t.time() - T0:.1f}초)")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
