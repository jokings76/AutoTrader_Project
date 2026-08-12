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

# 🔴 (2026-08-11) 이 감사는 **손절·청산 경로**를 본다. -3% 물타기가 켜져 있으면
#    손절선(-4.5%)보다 먼저 개입해 평단을 낮추므로 손절이 안 난다(의도된 동작).
#    물타기 자체는 test_patch_20260811.py에서 검증한다. 기본값은 아래에서 확인.
_AVGDOWN_DEFAULT = SM.AVG_DOWN_ENABLED
SM.AVG_DOWN_ENABLED = False
import core.daily_backtest as DB                # noqa: E402
import core.slot_replacement as SR              # noqa: E402
from core.strategy.portfolio_optimizer import DEFAULT_BASE_AMOUNT   # noqa: E402
from core.order_manager import (  # noqa: E402
    BUY_AMOUNT_PER_STOCK, FORCE_CLOSE_TIME, FORCE_CLOSE_ENABLED,
)
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
# 금액 자체는 사용자 결정 사항이라 값을 못박지 않는다(08-05 200만 -> 08-06 100만).
# 감사가 지켜야 할 불변식은 **3곳이 어긋나지 않는 것**이다 — 실제 사고가 그거였다.
check("매수금액 3곳 일치",
      SM.POSITION_AMOUNT == DEFAULT_BASE_AMOUNT == BUY_AMOUNT_PER_STOCK,
      f"{SM.POSITION_AMOUNT:,} / {DEFAULT_BASE_AMOUNT:,} / {BUY_AMOUNT_PER_STOCK:,}")
check("버스트 주가 스케일 상수",
      (SM.BURST_PRICE_REF, SM.BURST_PRICE_ALPHA,
       SM.BURST_PRICE_MIN, SM.BURST_PRICE_MAX) == (10_000.0, 0.55, 0.30, 3.00),
      f"{SM.BURST_PRICE_REF}/{SM.BURST_PRICE_ALPHA}/{SM.BURST_PRICE_MIN}/{SM.BURST_PRICE_MAX}")
check("재매수 배수 2.5", SM.REBUY_BURST_VALUE_MULT == 2.5)
check("VI 상수", (SM.VI_UPPER_EXIT_ENABLED, SM.VI_STATIC_RATIO,
                 SM.VI_UPPER_MARGIN_PCT, SM.VI_UPPER_MARGIN_TICKS)
      == (True, 0.10, 0.005, 2))

# 상한 클램프가 걸리는 주가 (오해 방지용 — MAX만 보면 안 된다)
bind = 10_000 * SM.BURST_PRICE_MAX ** (1 / SM.BURST_PRICE_ALPHA)
check("상한 클램프 시작 주가 73,704원 부근", 73_000 <= bind <= 74_500, f"{bind:,.0f}원")

# 실제 문턱표 — 수치를 눈으로 확인할 수 있게 출력
print("\n  주가별 실효 문턱:")
for px in (1_000, 2_000, 5_000, 10_000, 20_000, 32_600, 73_704, 113_900, 150_000):
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
# 🔴 죽은 슬롯 불변식 (2026-08-06 [E]) — 어느 한쪽 캡을 0으로 내렸을 때
#    나머지 캡이 공유 상한을 못 채우면 그 차이만큼 슬롯이 영구히 논다.
#    [E]에서 실제로 밟을 뻔한 함정이라 감사가 상시 감시한다.
check("죽은 슬롯 없음 — 캡 합이 공유 상한 이상",
      SM.PHASE1A_MAX_SLOTS + SM.PULLBACK_MAX_SLOTS >= SM.MAX_HOLDINGS,
      f"{SM.PHASE1A_MAX_SLOTS}+{SM.PULLBACK_MAX_SLOTS} vs {SM.MAX_HOLDINGS}")
check("확장 슬롯 도달 가능 — 캡 합이 하드 상한 이상",
      SM.PHASE1A_MAX_SLOTS + SM.PULLBACK_MAX_SLOTS >= SM.MAX_HOLDINGS_HARD)
# (2026-08-06) 파동 쿨다운이 요구 숙성보다 길면, 첫 파동이 카운트되기도 전에
#    숙성이 끝나 순번 판정이 무의미해지는 구간이 생긴다. 둘의 관계를 못박는다.
check("파동 쿨다운 <= 진입 숙성 (순번이 숙성보다 늦게 세어지지 않는다)",
      SM.BURST_WAVE_COOLDOWN_SEC <= max(SM.MIN_ENTRY_DELAY_SEC, 1e-9)
      or SM.MIN_ENTRY_DELAY_SEC <= 0,
      f"쿨다운 {SM.BURST_WAVE_COOLDOWN_SEC} vs 숙성 {SM.MIN_ENTRY_DELAY_SEC}")
check("파동 상한 >= 1 (0이면 아무것도 못 산다)", SM.BURST_WAVE_MAX >= 1)
# (2026-08-08) 개장 초반 슬롯 캡 — 공유 상한 이상이면 있으나 마나이고,
# 0이면 초반에 아무것도 못 산다. 발사 면제 구간과 시각이 어긋나도 안 된다.
check("초반 슬롯 캡 < 공유 상한 (실효성)",
      (not SM.EARLY_SLOT_CAP_ENABLED) or SM.EARLY_SLOT_CAP < SM.MAX_HOLDINGS,
      f"{SM.EARLY_SLOT_CAP} vs {SM.MAX_HOLDINGS}")
check("초반 슬롯 캡 >= 1", (not SM.EARLY_SLOT_CAP_ENABLED) or SM.EARLY_SLOT_CAP >= 1)
# (2026-08-08) 게이트 검사가격 == 실제 체결가 원칙. 되돌림이 켜져 있는데
# 깊은 검사가 꺼져 있으면 '계획만 열리고 체결 안 되는' 죽은 구간이 생긴다.
check("되돌림 ON이면 VWAP 깊은검사도 ON (죽은 구간 방지)",
      (not SM.ENTRY_PULLBACK_ENABLED) or (not SM.VWAP_ENTRY_ENABLED)
      or SM.VWAP_ENTRY_CHECK_DEEPEST,
      f"되돌림={SM.ENTRY_PULLBACK_ENABLED} VWAP={SM.VWAP_ENTRY_ENABLED} "
      f"깊은검사={SM.VWAP_ENTRY_CHECK_DEEPEST}")
# (2026-08-10) 원래 `> 0`이었다. 그런데 0은 상수 주석이 명시한 **정상 상태**
# ('0으로 두면 우선순위 교체가 완전히 꺼진다')이고, 오늘 실제로 0으로 껐다
# (115초에 6회 소진 + 3회를 유령에 낭비). 음수만 막는다.
check("우선순위 교체 일일 한도 >= 0 (0 = 기능 OFF, 정상)",
      SM.PHASE1A_PRIORITY_MAX_PER_DAY >= 0,
      f"{SM.PHASE1A_PRIORITY_MAX_PER_DAY}"
      + (" — 교체 OFF" if SM.PHASE1A_PRIORITY_MAX_PER_DAY == 0 else ""))
check("초반 캡 종료 == 발사 게이트 전환 시각 (구간이 어긋나지 않는다)",
      SM.EARLY_SLOT_CAP_UNTIL == SM.FIRE_GATE_ACCEL_FROM,
      f"{SM.EARLY_SLOT_CAP_UNTIL} / {SM.FIRE_GATE_ACCEL_FROM}")
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
# (2026-08-06) 매수차단 폭 > 매도발동 폭이어야 한다. 뒤집히면 **사자마자
# 되파는** 구조가 된다(매수는 허용되는데 그 가격이 이미 확정매도 구간).
check("🔴 VI 매수차단 폭 > VI 확정매도 폭 (사자마자 되팔기 방지)",
      SM.VI_UPPER_ENTRY_BLOCK_PCT > SM.VI_UPPER_MARGIN_PCT,
      f"매수차단 {SM.VI_UPPER_ENTRY_BLOCK_PCT} vs 매도 {SM.VI_UPPER_MARGIN_PCT}")
# (2026-08-06) 시가대비 밴드가 뒤집히면 '강화 구간'이 사라지거나 역전된다.
check("🔴 급등강화 시작 < 시가대비 매수보류 상한",
      SM.PHASE1A_OPEN_SURGE_STRICT_FROM < SM.PHASE1A_LEADING_OPEN_SURGE_CAP,
      f"강화 {SM.PHASE1A_OPEN_SURGE_STRICT_FROM}% < 보류 {SM.PHASE1A_LEADING_OPEN_SURGE_CAP}%")
check("🔴 급등강화 배수 >= 1.0 (완화 방향으로 못 간다)",
      SM.PHASE1A_OPEN_SURGE_BURST_MULT >= 1.0,
      str(SM.PHASE1A_OPEN_SURGE_BURST_MULT))
check("VI 매수차단 폭 < 정적VI 폭",
      SM.VI_UPPER_ENTRY_BLOCK_PCT < SM.VI_STATIC_RATIO)
check("추가매수 관찰 하한 > 손절선 (구조 전 더 내려갈 여지)",
      SM.RESCUE_ADD_OBSERVE_FLOOR > abs(SM.STOP_LOSS_RATE))
check("🆕 -3% 물타기 기본값 ON (감사 중에만 꺼둔다)",
      _AVGDOWN_DEFAULT is True, str(_AVGDOWN_DEFAULT))
check("물타기 문턱 < 손절선 (물타기가 손절보다 먼저 온다)",
      SM.AVG_DOWN_TRIGGER < abs(SM.STOP_LOSS_RATE),
      f"{SM.AVG_DOWN_TRIGGER} < {abs(SM.STOP_LOSS_RATE)}")
check("구조 후 최종선 > 물타기 후 평단손절 (원가 -6%가 진짜 백스톱)",
      SM.RESCUE_ADD_FINAL_STOP > 0, str(SM.RESCUE_ADD_FINAL_STOP))
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
    # ⚠️ 실물은 `update_sell(trade_id, ...)`로 **trade_id가 위치 인자**다. 키워드 전용으로
    # 두면 실물이 정상인데 스텁이 TypeError를 내고, 호출부의 except가 그걸 삼켜
    # **감사가 조용히 거짓말한다**(2026-08-10에 실제로 밟은 함정).
    def update_sell(cls, trade_id=None, **kw):
        cls.sells.append({"trade_id": trade_id, **kw})
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
r = sells_of(lambda: pos_at(10_000).on_price_update("Z", int(10_000 * (1 + SM.STOP_LOSS_RATE)) + 5))
check("🔴 손절선 직전에서 손절하지 않음", not any("손절" in x for x in r), str(r))
# 손절선 도달 -> 반드시 나간다 (추가매수 조건 미충족 시)
r = sells_of(lambda: pos_at(10_000).on_price_update("Z", int(10_000 * (1 + SM.STOP_LOSS_RATE)) - 1))
check("✅ 손절선 도달 시 반드시 청산", any("손절" in x for x in r), str(r))
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
s3.on_price_update("Z", int(10_000 * (1 + SM.STOP_LOSS_RATE)) - 1)
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
    # (2026-08-12) 이 매트릭스는 **어느 규칙이 판정을 가져가는가**(우선순위)를
    # 본다. VI 확정매도가 50% 분할이면 포지션이 안 닫혀 update_sell을 안 부르고
    # 사유가 DB에 안 남는다 -> 우선순위 판정이 불가능해진다. 그래서 여기서만
    # 분할을 끈다. 분할 동작 자체는 test_patch_20260813 [3]이 검증한다.
    _sv_vip = SM.VI_UPPER_EXIT_PARTIAL
    SM.VI_UPPER_EXIT_PARTIAL = False
    try:
        s.on_price_update("P", price)
    finally:
        SM.VI_UPPER_EXIT_PARTIAL = _sv_vip
    why = " | ".join(x.get("exit_reason") or "" for x in _Repo.sells) or "(보유유지)"
    print(f"    {label:<44} -> {why[:56]}")
    return why


w = scenario("손절 + VI 근접 + 가드", int(10_000 * (1 + SM.STOP_LOSS_RATE)) - 1, buy=10_000, open_px=8_900, guard=True)
check("손절이 최우선", "손절" in w, w[:50])
w = scenario("가드 + VI 근접 (이익)", 10_960, buy=10_000, open_px=10_000, guard=True)
check("가드가 VI보다 우선", "지수 가드" in w, w[:50])
w = scenario("VI 근접만 (이익, 캡 미달)", 11_840, buy=11_500, open_px=10_800)
check("VI가 캡보다 먼저 확정", "VI 상단" in w, w[:60])
# (2026-08-10) 캡 상향(4.0 -> 6.0)에 맞춰 가격을 상수에서 역산한다.
_capx = int(10_000 * (1 + SM.TAKE_PROFIT_CAP + SM.ROUND_TRIP_COST) + 50)
w = scenario("일반 익절캡 도달", _capx, buy=10_000)
check("VI 조건 없으면 익절캡", "익절" in w and "VI" not in w, f"{_capx} -> {w[:44]}")
w = scenario("워밍업 중 손절", int(10_000 * (1 + SM.STOP_LOSS_RATE)) - 1, buy=10_000, warm=True)
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


# ──────────────────────────────────────────────────────────────
section("[9] 📄 문서 ↔ 코드 정합성 (CLAUDE.md 기대값 블록)")
# ──────────────────────────────────────────────────────────────
# [왜 이 섹션이 있나] 이 프로젝트의 반복 사고 1위가 "값을 고치고 문서를 안
# 고치는 것"이다. 08-05 이관 때만 해도 체크리스트 숫자 2개(#2 287 / #5 63)가
# 실제와 어긋나 있었다. 문서가 틀리면 다음 세션이 **잘못된 값을 근거로
# 작업하거나 "회귀가 났다"고 오판하고 멀쩡한 코드를 뒤진다.**
# -> CLAUDE.md의 '기대값' 블록에 **라이브 값이 문자열 그대로 들어있는지**
#    기계적으로 확인한다. 상수를 바꾸고 문서를 안 고치면 여기서 잡힌다.
try:
    _doc = open("CLAUDE.md", encoding="utf-8").read()
except Exception as e:
    _doc = ""
    check("CLAUDE.md 읽기", False, str(e))

if _doc:
    def doc_has(label, s):
        ok = s in _doc
        check(f"문서에 {label} = `{s}`", ok,
              "" if ok else "❌ 문서에 없음 -> CLAUDE.md 기대값 블록을 갱신할 것")

    doc_has("시간창(1A)", f"1A {SM.GROUP_A_START} ~ {SM.PHASE1A_END}")
    doc_has("시간창(PB)", f"PB {SM.PULLBACK_START} ~ {SM.PULLBACK_END}")
    doc_has("중복전환", f"중복전환 {SM.DUAL_SOURCE_PULLBACK_FROM}")
    doc_has("청산시각", f"청산 {FORCE_CLOSE_TIME}")
    doc_has("슬롯", f"슬롯 {SM.PHASE1A_MAX_SLOTS} {SM.PULLBACK_MAX_SLOTS} "
                   f"{SM.MAX_HOLDINGS} {SM.MAX_HOLDINGS_HARD}")
    doc_has("진입숙성", f"진입숙성 {SM.MIN_ENTRY_DELAY_SEC}")
    doc_has("파동상한", f"파동상한 {SM.BURST_WAVE_MAX}")
    # 2026-08-09 결함수정 — 면제 구간에서 파동이 '시간'으로 소진되던 것을 막는다.
    doc_has("파동카운트", f"파동카운트 {SM.BURST_WAVE_COUNT_REQUIRES_BURST}")
    doc_has("무장", f"무장 {SM.TICK_STRENGTH_MIN} {SM.TICK_STRENGTH_SUSTAIN_SEC}")
    # 2026-08-07 신설. 현재 OFF지만 켜는 순간 매수 게이트가 되므로 문서와 묶어둔다.
    doc_has("VWAP진입", f"VWAP진입 {SM.VWAP_ENTRY_ENABLED} "
                       f"{SM.VWAP_ENTRY_MIN_GAP_PCT} {SM.VWAP_ENTRY_FROM}")
    doc_has("버스트", f"버스트 {SM.PHASE1A_BURST_TRADE_VALUE} "
                    f"{SM.PHASE1A_BURST_TRADE_COUNT} {SM.PHASE1A_SINGLE_TRADE_VALUE}")
    doc_has("주가계수", f"주가계수 {SM.BURST_PRICE_REF} {SM.BURST_PRICE_ALPHA} "
                     f"{SM.BURST_PRICE_MIN} {SM.BURST_PRICE_MAX}")
    doc_has("재매수배수", f"재매수배수 {SM.REBUY_BURST_VALUE_MULT}")
    doc_has("되돌림", f"되돌림 {SM.ENTRY_PULLBACK_ENABLED} "
                    f"{SM.ENTRY_PULLBACK_TRANCHES} {SM.ENTRY_PULLBACK_TIMEOUT_SEC}")
    doc_has("분할매도", f"분할매도 {SM.PARTIAL_EXIT_ENABLED} "
                     f"{SM.PARTIAL_EXIT_FRACTION} {SM.PARTIAL_EXIT_TRAIL}")
    doc_has("지수가드", f"지수가드 {SM.INDEX_GUARD_THRESHOLD} {SM.INDEX_GUARD_FROM} "
                     f"{SM.INDEX_GUARD_BREAKEVEN_UNTIL} {SM.INDEX_GUARD_FORCE_CLOSE}")
    doc_has("캡", f"캡 {SM.TAKE_PROFIT_CAP} {SM.TAKE_PROFIT_CAP_PULLBACK} "
                f"{SM.TAKE_PROFIT_CAP_EARLY} {SM.TP_CAP_UPGRADED_MAX}")
    doc_has("본전스톱", f"본전스톱 {SM.BREAKEVEN_STOP_ENABLED}")
    doc_has("VI", f"VI {SM.VI_UPPER_EXIT_ENABLED} {SM.VI_STATIC_RATIO} "
                 f"{SM.VI_UPPER_MARGIN_PCT} {SM.VI_UPPER_MARGIN_TICKS}")
    doc_has("VI매수차단", f"VI매수차단 {SM.VI_UPPER_ENTRY_BLOCK_ENABLED} "
                       f"{SM.VI_UPPER_ENTRY_BLOCK_PCT}")
    doc_has("시가대비/급등강화",
            f"시가대비 {SM.PHASE1A_LEADING_OPEN_SURGE_CAP}   "
            f"급등강화 {SM.PHASE1A_OPEN_SURGE_STRICT_FROM} "
            f"{SM.PHASE1A_OPEN_SURGE_BURST_MULT}")
    doc_has("매수금액", f"매수금액 3곳 {SM.POSITION_AMOUNT} "
                     f"{DEFAULT_BASE_AMOUNT} {BUY_AMOUNT_PER_STOCK} 일치")
    # (2026-08-11 신규) 손절 계층 / 물타기 / 조건식 — 이 셋이 오늘 바뀐 축이다.
    # ⚠️ 08-11에 손절·물타기를 바꿨는데 감사가 통과했다 — 여기에 대조 항목이
    #    **없었기 때문**이다. 새 상수를 넣으면 이 블록에도 반드시 추가할 것.
    doc_has("변동성손절", f"변동성손절 {SM.STOP_LOSS_VOL_ENABLED} "
                       f"{SM.STOP_LOSS_VOL_WINDOW_SEC} {SM.STOP_LOSS_VOL_MIN_TICKS} "
                       f"{SM.STOP_LOSS_VOL_MULT} {SM.STOP_LOSS_VOL_MIN} "
                       f"{SM.STOP_LOSS_VOL_MAX}")
    doc_has("고정손절", f"고정손절 {SM.STOP_LOSS_RATE}")
    doc_has("추가매수관찰", f"추가매수관찰 {SM.RESCUE_ADD_OBSERVE_SEC} "
                        f"{SM.RESCUE_ADD_OBSERVE_FLOOR} {SM.RESCUE_ADD_FINAL_STOP}")
    # ⚠️ 이 감사는 손절 경로를 보려고 AVG_DOWN_ENABLED를 꺼둔다. 그러니
    #    **꺼둔 값이 아니라 기본값**(_AVGDOWN_DEFAULT)과 문서를 대조해야 한다.
    #    안 그러면 "문서가 틀렸다"는 거짓 실패가 난다(실제로 한 번 났다).
    doc_has("물타기", f"물타기 {_AVGDOWN_DEFAULT} {SM.AVG_DOWN_TRIGGER} "
                   f"{SM.AVG_DOWN_MAX_AMOUNT} {SM.AVG_DOWN_BLOCK_ON_INDEX_GUARD}")
    doc_has("확인전용", f"확인전용 {tuple(SM.CONFIRM_ONLY_CONDITIONS)}")
    doc_has("숙성조건식별", f"숙성조건식별 {SM.MIN_ENTRY_DELAY_SEC_BY_COND}")

    # (2026-08-12 신규) 🔴 **시간 기반 자동청산 전면 폐지**와 물타기 컷오프.
    #    이 셋은 "봇이 언제 파는가"를 통째로 바꾼 축이라, 문서와 어긋나면
    #    장 마감 후 "왜 안 팔렸지?"를 판단할 수 없다.
    doc_has("물타기컷오프", f"물타기컷오프 {SM.AVG_DOWN_CUTOFF} "
                        f"{SM.AVG_DOWN_MAX_RETRY} {SM.AVG_DOWN_RETRY_COOLDOWN_SEC}")
    doc_has("강제청산", f"강제청산 {FORCE_CLOSE_ENABLED} {FORCE_CLOSE_TIME}")
    doc_has("지수가드강제청산", f"지수가드강제청산 {SM.INDEX_GUARD_FORCE_CLOSE_ENABLED}")
    doc_has("오버나이트격리", f"오버나이트격리 {SM.OVERNIGHT_RESTORE_AS_MANUAL}")

    # 🔴 불변식 — 시간 기반 청산이 전부 닫혔다면, 문서의 청산표도 그렇게
    #    말해야 한다. 하나만 되살리고 문서를 안 고치면 여기서 잡힌다.
    if not (FORCE_CLOSE_ENABLED or SM.INDEX_GUARD_FORCE_CLOSE_ENABLED):
        check("문서가 '시간 기반 자동청산 없음'을 명시",
              "시간 기반 자동청산" in _doc,
              "❌ 강제청산을 껐으면 문서 청산표/요약도 갱신할 것")

    # (2026-08-12 장마감 후 신규 5건) 진입 유효창 / VI 분할 / 잔량 보호 /
    # 편입가 앵커. 매수·매도 시점을 직접 바꾸는 값이라 문서와 어긋나면
    # 장중에 "왜 안 사지/왜 벌써 팔지"를 판단할 수 없다.
    doc_has("진입유효창", f"진입유효창 {SM.MAX_ENTRY_AGE_SEC}")
    doc_has("VI분할", f"VI분할 {SM.VI_UPPER_EXIT_PARTIAL}")
    doc_has("분할잔량보호", f"분할잔량보호 {SM.PARTIAL_EXIT_REMAINDER_HOLD}")
    doc_has("편입가앵커", f"편입가앵커 {SM.ENTRY_ANCHOR_SECOND_TRANCHE}")

    # 🔴 불변식 — 진입 유효창은 숙성보다 커야 창이 열린다.
    if SM.MAX_ENTRY_AGE_SEC > 0:
        check("진입 유효창 상한 > 숙성 하한 (창이 닫히지 않았다)",
              SM.MAX_ENTRY_AGE_SEC > SM.MIN_ENTRY_DELAY_SEC,
              f"{SM.MAX_ENTRY_AGE_SEC} > {SM.MIN_ENTRY_DELAY_SEC}")
        check("진입 유효창 상한 > 조건식별 숙성 최대값",
              SM.MAX_ENTRY_AGE_SEC > max(
                  [SM.MIN_ENTRY_DELAY_SEC] + list(SM.MIN_ENTRY_DELAY_SEC_BY_COND.values())))

    # (2026-08-06 신규) [B][C][D]
    doc_has("등락률하한", f"등락률하한 {SM.MIN_ENTRY_CHANGE_PCT}")
    doc_has("상승이탈", f"상승이탈 {SM.ENTRY_BREAKOUT_ENABLED} {SM.ENTRY_BREAKOUT_PCT}")
    doc_has("버스트방향", f"버스트방향 {SM.BURST_REQUIRE_BUY_SIDE}")
    # (2026-08-08 신규) 발사 게이트 시간대 분리 — 매수를 만드는 조건이 통째로
    # 바뀌는 상수라 문서와 어긋나면 장중 판단이 불가능해진다.
    doc_has("초반캡", f"초반캡 {SM.EARLY_SLOT_CAP_ENABLED} "
                    f"{SM.EARLY_SLOT_CAP_UNTIL} {SM.EARLY_SLOT_CAP}")
    doc_has("VWAP깊은검사", f"VWAP깊은검사 {SM.VWAP_ENTRY_CHECK_DEEPEST} "
                          f"{SM.PHASE1A_PRIORITY_MAX_PER_DAY}")
    doc_has("발사분리", f"발사분리 {SM.FIRE_GATE_SPLIT_ENABLED} "
                     f"{SM.FIRE_GATE_ACCEL_FROM} {SM.FIRE_ACCEL_MIN} "
                     f"{SM.FIRE_ACCEL_SHORT_SEC} {SM.FIRE_ACCEL_LONG_SEC} "
                     f"{SM.FIRE_ACCEL_MIN_TICKS}")

    # 주가계수 예시표 — 문서에 적힌 배수가 실제와 같은가
    for px in (1_000, 2_000, 10_000, 50_000, 150_000):
        doc_has(f"계수({px:,}원)", f"{px:,}원 x{SM.burst_price_scale(px):.2f}")

    # 등락률 상한 / 익절캡 — '현재 전략 요약' 표의 수치
    check(f"문서 등락률 상한 1A = {SM.MAX_ENTRY_CHANGE_PCT:.0f}%",
          f"**{SM.MAX_ENTRY_CHANGE_PCT:.0f}%**" in _doc)
    check(f"문서 등락률 상한 눌림 = {SM.MAX_ENTRY_CHANGE_PCT_PULLBACK:.0f}%",
          f"**{SM.MAX_ENTRY_CHANGE_PCT_PULLBACK:.0f}%**" in _doc)
    check(f"문서 익절캡 1A = {SM.TAKE_PROFIT_CAP*100:.1f}%",
          f"1A {SM.TAKE_PROFIT_CAP*100:.1f}%" in _doc)
    check("문서 상한 클램프 시작 주가 표기",
          f"{10_000 * SM.BURST_PRICE_MAX ** (1/SM.BURST_PRICE_ALPHA):,.0f}원" in _doc)
    # VI 규칙이 청산 우선순위 표에 실려 있는가
    check("문서 청산표에 VI 항목 존재", "VI 상단 확정매도" in _doc)
    check("문서에 VI 마진 표기", f"{SM.VI_UPPER_MARGIN_PCT*100:.1f}% 이내" in _doc)
    check("문서에 VI 기준 표기(시가 x1.10)",
          f"시가 x{1+SM.VI_STATIC_RATIO:.2f}" in _doc)
    # 실전 여부
    from config import settings as _st
    check(f"문서 IS_MOCK 기대값이 실제({_st.IS_MOCK})와 일치",
          f"IS_MOCK {_st.IS_MOCK}" in _doc, f"실제 IS_MOCK={_st.IS_MOCK}")


# ──────────────────────────────────────────────────────────────
section("[10] 🔬 청산 규칙 상호 충돌 매트릭스")
# ──────────────────────────────────────────────────────────────
# 각 규칙을 '단독으로 성립'시키고, 다른 규칙이 끼어들어 **엉뚱한 사유로**
# 나가지 않는지 본다. 08-03에 동적캡이 손실 구간에서 발동한 것, 08-05에
# 시간정리가 지수가드 사양을 무력화한 것이 전부 이 부류였다.
def one(label, expect, **kw):
    price = kw.pop("price")
    buy = kw.pop("buy", 10_000)
    s, _ = build(kw.pop("now", datetime(2026, 8, 5, 10, 0, 0)))
    p = put_pos(s, "M", buy=buy, warm=kw.pop("warm", False))
    for k, v in kw.pop("pos", {}).items():
        p[k] = v
    if kw.pop("open_px", None):
        s._opening_prices["M"] = kw.pop("_o", None)
    for k, v in kw.items():
        setattr(s, k, v)
    _Repo.sells = []
    s.on_price_update("M", price)
    why = " | ".join(x.get("exit_reason") or "" for x in _Repo.sells) or "(보유유지)"
    ok = (expect in why) if expect else (why == "(보유유지)")
    check(f"{label} -> {expect or '보유유지'}", ok, why[:58])


one("손절만 성립", "손절", price=int(10_000 * (1 + SM.STOP_LOSS_RATE)) - 1)
# (2026-08-10) 캡 상향(4.0 -> 6.0)에 맞춰 상수에서 역산한다.
one("익절캡만 성립", "익절",
    price=int(10_000 * (1 + SM.TAKE_PROFIT_CAP + SM.ROUND_TRIP_COST) + 50))
one("아무것도 성립 안 함", None, price=10_100)
one("워밍업 중 + 손절", "손절", price=int(10_000 * (1 + SM.STOP_LOSS_RATE)) - 1, warm=True)
one("워밍업 중 + 익절 구간", None, price=10_450, warm=True)
one("손절선 1원 위", None, price=int(10_000 * (1 + SM.STOP_LOSS_RATE)) + 1)
one("익절캡 1원 아래", None, price=10_399)

# VI: 시가를 넣어야 성립 — 별도 구성
def one_vi(label, expect, open_px, buy, price, **kw):
    s, _ = build(kw.pop("now", datetime(2026, 8, 5, 10, 0, 0)))
    put_pos(s, "M", buy=buy, warm=kw.pop("warm", False))
    s._opening_prices["M"] = open_px
    for k, v in kw.items():
        setattr(s, k, v)
    _Repo.sells = []
    # (2026-08-12) scenario()와 같은 이유로 VI 분할을 끄고 잰다 —
    # 여기서 보는 것은 '어느 사유로 나가는가'이지 분할 여부가 아니다.
    _sv_vip = SM.VI_UPPER_EXIT_PARTIAL
    SM.VI_UPPER_EXIT_PARTIAL = False
    try:
        s.on_price_update("M", price)
    finally:
        SM.VI_UPPER_EXIT_PARTIAL = _sv_vip
    why = " | ".join(x.get("exit_reason") or "" for x in _Repo.sells) or "(보유유지)"
    ok = (expect in why) if expect else (why == "(보유유지)")
    check(f"{label} -> {expect or '보유유지'}", ok, why[:58])


one_vi("VI 근접 + 캡 미달", "VI 상단", 10_800, 11_500, 11_840)
# 손절선이 상수라 가격을 역산한다 (매수 13,600 / 손절선 아래로).
one_vi("VI 근접 + 손절 동시", "손절", 12_000, 13_600,
       int(13_600 * (1 + SM.STOP_LOSS_RATE)) - 1)
one_vi("VI 근접인데 순손실", None, 10_800, 12_000, 11_840)
one_vi("VI 멀고 캡도 미달", None, 10_000, 10_000, 10_200)
one_vi("VI 근접 + 워밍업", "VI 상단", 10_800, 11_500, 11_840, warm=True)

# 지수가드가 다른 규칙을 덮는가 (익절캡보다 먼저)
s, _ = build(datetime(2026, 8, 5, 11, 10, 0))
put_pos(s, "M", buy=10_000)
s._is_index_guard_active = lambda now_dt=None: True
_Repo.sells = []
s.on_price_update("M", 10_450)          # 익절캡도 성립하는 가격
why = " | ".join(x.get("exit_reason") or "" for x in _Repo.sells)
check("가드 중엔 익절캡보다 가드가 먼저", "지수 가드" in why, why[:58])

# 가드 중 손실 포지션은 11:30 전엔 안 판다 (사양)
s, _ = build(datetime(2026, 8, 5, 11, 10, 0))
put_pos(s, "M", buy=10_000)
s._is_index_guard_active = lambda now_dt=None: True
_Repo.sells = []
s.on_price_update("M", 9_900)           # 손실이지만 손절선 위
why = " | ".join(x.get("exit_reason") or "" for x in _Repo.sells) or "(보유유지)"
check("가드 중 손실분은 11:30 전 보유유지", why == "(보유유지)", why[:58])


# ──────────────────────────────────────────────────────────────
section("[11] 🔬 매수 게이트 순서 — 하나라도 빠지면 잘못된 매수")
# ──────────────────────────────────────────────────────────────
_src_buy = inspect.getsource(SM.StrategyManager._execute_buy)
for label, needle in [
    ("15:10 하드컷오프", "ENTRY_HARD_CUTOFF"),
    ("전략 라우팅 가드", "1A_눌림"),
    ("지수 하락 가드", "_is_index_guard_active"),
    ("pending 회수(finally)", "finally"),
]:
    check(f"_execute_buy에 {label} 존재", needle in _src_buy)

# ⚠️ 등락률 게이트는 **소스 문자열로 찾으면 안 된다.** 2026-08-06에 상·하한을
#    `_entry_change_reject()` 한 함수로 모으면서 `_entry_change_cap`이라는
#    문자열이 _execute_buy에서 사라졌고, 이 감사가 **멀쩡한 코드를 실패로
#    보고했다**(이 파일이 스스로 경고하던 오탐 패턴을 그대로 밟았다).
#    -> 실제 호출·반환값으로 검증한다.
_chg_called = {"n": 0}
_orig_chg = SM.StrategyManager._entry_change_reject
try:
    def _spy(self, code, sub, price):
        _chg_called["n"] += 1
        return _orig_chg(self, code, sub, price)
    SM.StrategyManager._entry_change_reject = _spy
    s_c, _ = build()
    s_c._stock_names["CG"] = "CG"
    s_c._cond_names["CG"] = "주도주상위"
    s_c.phase1b.start_watching("CG")
    s_c._execute_buy("CG", "CG", 1, {"current_price": 10_000}, sub_strategy="1A")
finally:
    SM.StrategyManager._entry_change_reject = _orig_chg
check("_execute_buy가 등락률 게이트(상·하한)를 실제로 호출한다",
      _chg_called["n"] > 0, f"호출 {_chg_called['n']}회")

# 상·하한이 **한 함수에** 모여 있는지 — 규칙 분산이 이 코드베이스의 반복 사고다
_s_chg, _ = build()
_s_chg.api.get_stock_change_rate = lambda c: 20.0
_s_chg.api.get_basic_quote = lambda c: {"change_rate": 20.0}
_s_chg._prev_closes = {}
_hi = _s_chg._entry_change_reject("X", "1A", 10_000)
_s_chg.api.get_stock_change_rate = lambda c: -2.0
_s_chg.api.get_basic_quote = lambda c: {"change_rate": -2.0}
_s_chg._prev_closes = {}
_lo = _s_chg._entry_change_reject("Y", "1A", 10_000)
check("같은 함수가 상한(+20%)을 거절", _hi is not None and "상한" in _hi, str(_hi))
check("같은 함수가 하한(-2%)을 거절", _lo is not None and "하한" in _lo, str(_lo))

_src_entry = inspect.getsource(SM.StrategyManager._maybe_tick_entry)
check("_maybe_tick_entry에 무장 확인 존재", "update_strength_timer" in _src_entry)
check("_maybe_tick_entry에 되돌림 계획 존재", "_open_entry_plan" in _src_entry)
# ⚠️ 발사 판정은 evaluate_tick_entry를 **거쳐** 호출된다. 소스 문자열로 찾으면
#    없다고 나온다(실제로 이 감사를 쓰다가 한 번 오탐을 냈다).
#    -> 소스가 아니라 **호출이 실제로 일어나는지**로 검증한다.
# (2026-08-08) 발사 조건이 시간대로 갈렸다 — check_burst 직접 호출이 아니라
#    `_fire_gate` 단일 창구를 거쳐야 한다. 여기서 check_burst를 그대로 단언하면
#    09:05 이전 면제 구간을 감사가 '회귀'로 오판한다.
_called = {"n": 0}
_orig = SM.StrategyManager._fire_gate
try:
    SM.StrategyManager._fire_gate = lambda self, *a, **k: (_called.__setitem__("n", _called["n"] + 1),
                                                           (False, {"reason": "감사스텁"}))[1]
    s_b, _ = build()
    s_b.phase1b.start_watching("CB")
    tfb = s_b.phase1b.trade_flow
    nwb = _t.time()
    for i in range(30):
        tfb.add_tick("CB", 10_000, "buy", 5, now=nwb - 60 + i)
    s_b._strength_since["CB"] = nwb - 10          # 무장 성립 상태로
    s_b._armed_at["CB"] = nwb - 5
    s_b.evaluate_tick_entry("CB", "1A", 10_000, now=nwb)
finally:
    SM.StrategyManager._fire_gate = _orig
check("진입 평가가 실제로 발사 게이트(_fire_gate)를 호출한다", _called["n"] > 0,
      f"호출 {_called['n']}회")

# 발사 게이트가 시간대로 갈리는지 — 실제 반환값으로 확인(소스 단언 금지)
_s_fg, _ = build()
_s_fg.phase1b.start_watching("FG")
_ok_early, _d_early = _s_fg._fire_gate(
    "FG", now=_t.time(), now_dt=datetime(2026, 8, 10, 9, 2, 0))
_ok_late, _d_late = _s_fg._fire_gate(
    "FG", now=_t.time(), now_dt=datetime(2026, 8, 10, 9, 6, 0))
check("09:05 이전은 발사 면제(틱 없이도 통과)", _ok_early is True,
      str(_d_early.get("trigger")))
check("09:05 이후는 가속도 판정(틱 없으면 탈락)",
      _ok_late is False and _d_late.get("fire_gate") == "accel",
      str(_d_late.get("reason"))[:60])
check("가속 문턱이 수학적 상한(LONG/SHORT) 미만 — 도달 불가 문턱 방지",
      SM.FIRE_ACCEL_MIN < SM.FIRE_ACCEL_LONG_SEC / SM.FIRE_ACCEL_SHORT_SEC,
      f"{SM.FIRE_ACCEL_MIN} < {SM.FIRE_ACCEL_LONG_SEC / SM.FIRE_ACCEL_SHORT_SEC}")
from core.strategy.trade_flow import TradeFlowTracker as _TFT   # noqa: E402
check("가속 LONG == 틱버퍼 창(조용한 절단 방지)",
      SM.FIRE_ACCEL_LONG_SEC == _TFT().max_window_sec,
      f"{SM.FIRE_ACCEL_LONG_SEC} vs {_TFT().max_window_sec}")
check("재매수 경로는 여전히 check_burst를 쓴다(발사 교체가 번지지 않았다)",
      "check_burst" in inspect.getsource(SM.StrategyManager._rebuy_after_loss_ok))

# 전면차단 사유 — 소스 문자열이 아니라 **반환값**으로 검증한다
check("전면차단 판정이 _entry_block_reason 단일 창구",
      hasattr(SM.StrategyManager, "_entry_block_reason"))
s_g, _ = build()
s_g._is_index_guard_active = lambda now_dt=None: True
check("차단사유: 지수가드", "지수 하락 가드" in (s_g._entry_block_reason() or ""),
      str(s_g._entry_block_reason()))
s_g2, _ = build()
s_g2.risk_can_trade = lambda: False
check("차단사유: MDD", "MDD" in (s_g2._entry_block_reason() or ""),
      str(s_g2._entry_block_reason()))
s_g3, _ = build()
s_g3.quarantine_until = s_g3._now() + timedelta(minutes=5)
check("차단사유: WS 재연결 격리", "격리" in (s_g3._entry_block_reason() or ""),
      str(s_g3._entry_block_reason()))
s_g4, _ = build()
check("정상 상태에선 차단사유 없음", s_g4._entry_block_reason() is None,
      str(s_g4._entry_block_reason()))

# 되돌림 계획이 슬롯을 점유하는가 (대기 중 자리 뺏김 방지)
check("occupied_slots가 _entry_plans를 센다",
      "_entry_plans" in inspect.getsource(SM.StrategyManager.occupied_slots))


print("\n" + "=" * 66)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건   ({_t.time() - T0:.1f}초)")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
