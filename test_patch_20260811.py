# -*- coding: utf-8 -*-
"""2026-08-11 변경 전용 스위트.

  [1] 손절 계층 -4.5 / -5.5 / -6.0 과 불변식
  [2] 조건식 개편 — 돌파전(매수) / 돌파후(확인 전용)
  [3] -3% 물타기 — 경계·1회·캡·안전게이트·복원분 제외·롤백
  [4] 구조 후 최종 방어선 — 원가 -6%가 진짜 하드 백스톱인가
  [5] 수동 추가매수 합산 (평단·수량·DB·본전스톱 재설정)
  [6] 수동매도 vs 미체결 구분

실행: PYTHONIOENCODING=utf-8 AUTOTRADER_TEST_LOG=1 python test_patch_20260811.py
"""
import os
import sys

os.environ.setdefault("AUTOTRADER_TEST_LOG", "1")

import inspect
import time as _t
from datetime import date as _date, datetime, timedelta

import core.strategy_manager as SM                      # noqa: E402
from core.phase1b_controller import Phase1BController    # noqa: E402
from config import settings                             # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


def section(t):
    print("\n" + "=" * 66)
    print(t)
    print("=" * 66)


class _Repo:
    rows, sells, updates = [], [], []

    @classmethod
    def find_holdings(cls): return []

    @classmethod
    def find_by_date(cls, d): return []

    @classmethod
    def insert_buy(cls, **kw): cls.rows.append(kw); return len(cls.rows)

    @classmethod
    def update_sell(cls, trade_id=None, **kw):
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
        return {"success": True, "ord_no": "1", "price": ref_price or price or 9_700,
                "style": order_style}

    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"side": "sell", "code": code, "qty": qty,
                            "style": order_style})
        return {"success": True, "ord_no": "2", "price": price or 9_700,
                "style": order_style}

    def get_stock_name(self, code): return code


# 🔴 픽스처 시각은 **추가매수 컷오프(ADD_BUY_CUTOFF)보다 앞**이어야 한다.
#    08-18에 컷오프가 11:00 -> 10:00으로 앞당겨지자 하드코딩된 10:00 픽스처가
#    통째로 컷오프에 걸려 물타기 테스트가 무더기로 실패했다 —
#    "테스트 픽스처에 수치를 하드코딩하지 말 것"의 교과서적 사례다.
#    상수에서 30분 역산해 다음에 또 바뀌어도 안 깨지게 한다.
NOW = datetime.combine(_date(2026, 8, 12), SM.ADD_BUY_CUTOFF) - timedelta(minutes=30)


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


def put(s, code="X", buy=10_000, qty=100, day_offset=0):
    s.holdings[code] = {
        "stock_code": code, "stock_name": code, "buy_price": buy,
        "buy_quantity": qty, "qty": qty,
        "buy_time": NOW - timedelta(days=day_offset, minutes=30),
        "warmup_until": NOW - timedelta(seconds=1),
        "sub_strategy": "1A", "strategy_phase": "1A", "trade_id": 1,
        "origin_price": buy, "lowest_price": buy, "highest_price": buy,
        "stop_rate": None,
    }
    s._prev_closes[code] = buy * 0.9
    s._opening_prices[code] = buy
    return s.holdings[code]


def buys(s): return [o for o in s.order_manager.orders if o["side"] == "buy"]
def sells(s): return [o for o in s.order_manager.orders if o["side"] == "sell"]


# ══════════════════════════════════════════════════════════
section("[1] 손절 계층 — -4.5% / -5.5% / -6.0% 과 불변식")
# ══════════════════════════════════════════════════════════
check("손절선 -4.5%", abs(SM.STOP_LOSS_RATE - (-0.045)) < 1e-9, str(SM.STOP_LOSS_RATE))
check("변동성 손절 하한 -4.5%", abs(SM.STOP_LOSS_VOL_MIN - (-0.045)) < 1e-9,
      str(SM.STOP_LOSS_VOL_MIN))
check("변동성 손절 상한 -5.5% (불변)", abs(SM.STOP_LOSS_VOL_MAX - (-0.055)) < 1e-9)
check("관찰 하한 5.5%", abs(SM.RESCUE_ADD_OBSERVE_FLOOR - 0.055) < 1e-9,
      str(SM.RESCUE_ADD_OBSERVE_FLOOR))
check("🔴 관찰바닥 > 손절선 (관찰 창이 0이 아니다)",
      SM.RESCUE_ADD_OBSERVE_FLOOR > abs(SM.STOP_LOSS_RATE),
      f"{SM.RESCUE_ADD_OBSERVE_FLOOR} > {abs(SM.STOP_LOSS_RATE)}")
check("최종선 > 관찰바닥", SM.RESCUE_ADD_FINAL_STOP > SM.RESCUE_ADD_OBSERVE_FLOOR)
check("클램프 방향 (MAX가 더 깊다)", SM.STOP_LOSS_VOL_MAX <= SM.STOP_LOSS_VOL_MIN)
check("물타기 문턱 < 손절선 (물타기가 먼저 온다)",
      SM.AVG_DOWN_TRIGGER < abs(SM.STOP_LOSS_RATE),
      f"{SM.AVG_DOWN_TRIGGER} < {abs(SM.STOP_LOSS_RATE)}")

# 실제 경계 — 실물 on_price_update에 태운다
for g, want in ((-0.0449, False), (-0.0451, True)):
    s = build(); put(s)
    _sv = SM.AVG_DOWN_ENABLED
    SM.AVG_DOWN_ENABLED = False        # 물타기가 가로채지 않게
    try:
        s.on_price_update("X", 10_000 * (1 + g))
    finally:
        SM.AVG_DOWN_ENABLED = _sv
    check(f"손절 경계 {g*100:+.2f}% -> {'발동' if want else '통과'}",
          ("X" not in s.holdings) is want)

# ══════════════════════════════════════════════════════════
section("[2] 조건식 개편 — 돌파전(매수) / 돌파후(확인 전용)")
# ══════════════════════════════════════════════════════════
check("CONDITION_NAMES에 돌파전·돌파후",
      settings.CONDITION_NAMES == ["주도주상위", "눌림목자동", "돌파전", "돌파후"],
      str(settings.CONDITION_NAMES))
check("숙성 예외 키가 돌파전", SM.MIN_ENTRY_DELAY_SEC_BY_COND == {"돌파전": 30.0},
      str(SM.MIN_ENTRY_DELAY_SEC_BY_COND))
check("CONFIRM_ONLY_CONDITIONS = ('돌파후',)",
      tuple(SM.CONFIRM_ONLY_CONDITIONS) == ("돌파후",))
check("확인전용도 구독 대상(CONDITION_NAMES에 포함)",
      all(c in settings.CONDITION_NAMES for c in SM.CONFIRM_ONLY_CONDITIONS))

S = SM.StrategyManager
for cn, want_1a in (("돌파전", True), ("돌파후", False), ("주도주상위", True)):
    check(f"source_flags[{cn}] 1A={want_1a}", S.source_flags(cn)[0] is want_1a)
for cn, blocked in (("돌파후", True), ("돌파전", False), ("돌파전+돌파후", False),
                    ("주도주상위+돌파후", False), ("눌림목자동", False), ("", False)):
    r = S._confirm_only_reject(cn)
    check(f"확인전용 판정 [{cn or '(빈값)'}] -> {'차단' if blocked else '통과'}",
          bool(r) is blocked, str(r)[:44])

# 🔴 resolve_strategy가 미분류를 "1A"로 폴백하므로, 차단은 별도 게이트가 한다.
check("🔴 resolve_strategy는 돌파후를 여전히 1A로 폴백한다(그래서 게이트가 필요)",
      S.resolve_strategy("돌파후", SM.GROUP_A_START) == "1A")
for fn in ("_open_entry_plan", "_execute_buy"):
    check(f"{fn}이 확인전용 게이트를 호출한다",
          "_confirm_only_reject" in inspect.getsource(getattr(S, fn)))

# ══════════════════════════════════════════════════════════
section("[3] -3% 물타기")
# ══════════════════════════════════════════════════════════
check("AVG_DOWN_ENABLED 기본 ON", SM.AVG_DOWN_ENABLED is True)
check("문턱 3%", abs(SM.AVG_DOWN_TRIGGER - 0.03) < 1e-9)
check("종목당 상한 200만원", SM.AVG_DOWN_MAX_AMOUNT == 2_000_000)

for px, want in ((9_710, False), (9_700, True), (9_690, True)):
    s = build(); p = put(s)
    s.on_price_update("X", px)
    check(f"물타기 경계 {px}원 ({(px-10000)/100:+.1f}%) -> {'발동' if want else '통과'}",
          bool(p.get("avg_down_done")) is want and (len(buys(s)) == 1) is want)

s = build(); p = put(s)
s.on_price_update("X", 9_700)
check("동일 주식수 추가", buys(s) and buys(s)[0]["qty"] == 100, str(buys(s)))
check("평단이 정확히 (10000+9700)/2", abs(p["buy_price"] - 9_850) < 1e-6,
      str(p["buy_price"]))
check("원가(origin_price)는 유지된다", p["origin_price"] == 10_000)
check("본전스톱 상태가 재설정된다",
      p.get("breakeven_armed") is False and p.get("breakeven_peak") == 0.0)
check("일일 카운터 증가", s._avg_down_count_today == 1)

s = build(); p = put(s)
for px in (9_700, 9_720, 9_700, 9_690):
    s.on_price_update("X", px)
check("포지션당 1회만", len(buys(s)) == 1, f"{len(buys(s))}건")

s = build(); p = put(s, day_offset=1)          # 전일 매수분(재시작 복원)
s.on_price_update("X", 9_700)
check("🔴 당일 매수분이 아니면 물타기 안 함(복원 포지션)",
      not buys(s) and p.get("avg_down_done") is True)

s = build(); p = put(s, qty=150)               # 150만원 기투입
s.on_price_update("X", 9_700)
_added = p["qty"] - 150
check("종목당 200만원 캡을 넘지 않는다",
      150 * 10_000 + _added * 9_700 <= SM.AVG_DOWN_MAX_AMOUNT,
      f"총 {150*10_000 + _added*9_700:,}원 / {_added}주 추가")

import types
s = build(); p = put(s)
s._is_index_guard_active = types.MethodType(lambda self: True, s)
s.on_price_update("X", 9_700)
check("🔴 지수 가드 발동 중엔 물타기 금지", not buys(s))

s = build(); p = put(s)
s._base_capital = 10_000_000
s._risk_tripped = True
s.on_price_update("X", 9_700)
check("🔴 MDD 일손실 차단 중엔 물타기 금지", not buys(s))

_sv = SM.AVG_DOWN_ENABLED
SM.AVG_DOWN_ENABLED = False
try:
    s = build(); p = put(s)
    s.on_price_update("X", 9_700)
    check("[롤백] AVG_DOWN_ENABLED=False면 발동 안 함", not buys(s))
finally:
    SM.AVG_DOWN_ENABLED = _sv
check("[롤백 후] 정상 복원", SM.AVG_DOWN_ENABLED is True)

# ══════════════════════════════════════════════════════════
section("[4] 구조 후 최종 방어선 — 원가 -6%가 진짜 하드 백스톱인가")
# ══════════════════════════════════════════════════════════
# 🔴 물타기 후 평단 9,850 -> 평단 기준 손절선은 9,407 = 원가 -5.93%.
#    최종선(원가 -6% = 9,400)이 그보다 **아래**라, 예전처럼 손절 분기 안에
#    두면 영영 도달하지 않는다. 밖으로 꺼냈는지 동작으로 확인한다.
for px, held in ((9_410, True), (9_400, False), (9_390, False)):
    s = build(); put(s)
    s.on_price_update("X", 9_700)      # 물타기
    s.on_price_update("X", px)
    why = (_Repo.sells[-1].get("exit_reason") or "") if _Repo.sells else ""
    check(f"원가 {(px-10000)/100:+.2f}% -> {'보유' if held else '청산'}",
          ("X" in s.holdings) is held,
          why[:40] if not held else "")
    if not held:
        check("  사유가 '구조 후 최종손절'", "최종손절" in why, why[:40])
        break

_src = inspect.getsource(SM.StrategyManager.on_price_update)
_i_final = _src.find("구조 후 최종손절")
_i_stop = _src.find("stop_rate = self.stop_loss_rate_for")
check("🔴 최종 방어선 검사가 손절 분기보다 **위**에 있다",
      0 < _i_final < _i_stop, f"final={_i_final} stop={_i_stop}")

# ══════════════════════════════════════════════════════════
section("[5] 수동 추가매수 합산 (main.py 잔고 대조)")
# ══════════════════════════════════════════════════════════
import main as M

_src_rec = inspect.getsource(M.TradingBot._reconcile_manual_sells)
check("수량 증가 분기가 있다", "server_qty > tracked_qty" in _src_rec)
check("서버 평단(avg_price)을 쓴다", "avg_price" in _src_rec)
check("2회 연속 관측 가드", "_pending_qty_up" in _src_rec)
check("되돌림 계획 중엔 보류", "_entry_plans" in _src_rec)
check("본전스톱 재설정", "breakeven_peak" in _src_rec)
# ⚠️ 단순 문자열 포함으로 보면 **주석의 'origin_price'**를 잡아 오탐이 난다
#    (이 프로젝트가 반복 경고한 함정 — 실제로 여기서 한 번 밟았다).
#    '대입이 있는가'로 본다.
check("origin_price를 덮어쓰지 않는다(최종 방어선 기준 보존)",
      'pos["origin_price"] =' not in _src_rec
      and "pos['origin_price'] =" not in _src_rec)
check("DB도 갱신한다", "TradeRepository.update(" in _src_rec)
check("체결 확인 표식을 남긴다(수동매도 구분용)", "seen_on_server" in _src_rec)

# ══════════════════════════════════════════════════════════
section("[6] 수동매도 vs 미체결 구분")
# ══════════════════════════════════════════════════════════
for seen, want in ((True, "수동 매도 정리"), (False, "미체결 포지션 정리")):
    s = build()
    p = put(s)
    if seen:
        p["seen_on_server"] = True
    _Repo.sells = []
    s._release_ghost_position("X", "테스트")
    why = (_Repo.sells[-1].get("exit_reason") or "") if _Repo.sells else ""
    check(f"seen_on_server={seen} -> '{want}'", want in why, why[:44])
    check("  DB가 닫힌다(재시작 부활 방지)", len(_Repo.sells) == 1)
    check("  슬롯이 반환된다", "X" not in s.holdings)

print("\n" + "=" * 66)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
if FAIL:
    print("\n실패 목록:")
    for f in FAIL:
        print("  -", f)
print("=" * 66)
sys.exit(1 if FAIL else 0)
