# -*- coding: utf-8 -*-
"""2026-08-08 심층 감사 — 발사 게이트 분리 + 초반 슬롯 캡이 전체와 유기적으로 도는가.

기존 감사(audit_20260805)가 '오늘 바뀐 값이 문서와 맞는가 / 경로별 오작동'을
본다면, 이 파일은 **변경이 만들어낸 새 상호작용**만 따로 훑는다.
사용자 요구: "매수/매도가 오류로 나가는 일이 절대 없어야 한다."

  [1] 진입 경로 2개가 **같은 게이트**를 지나는가 (한쪽만 고치는 사고 방지)
  [2] 매수를 **만드는** 경로 전수 — 어디로도 게이트를 우회할 수 없는가
  [3] 매도가 **잘못 나가지 않는가** — 오늘 변경이 청산에 손대지 않았음을 실증
  [4] '팔았는데 못 사는' 반쪽 동작 (08-03 -235,860원 사고의 재발 방지)
  [5] 우선순위 교체 폭주 한도 (초반 캡의 부작용)
  [6] 수치 정합성 — 문서/코드/실측 재계산이 세 곳 다 같은가
  [7] 종일 시나리오 — 09:00~15:10을 실제 코드로 관통
  [8] 이상 입력 내성 — 어떤 쓰레기가 들어와도 매수/매도가 새지 않는가

실행: PYTHONIOENCODING=utf-8 AUTOTRADER_TEST_LOG=1 python audit_deep_20260808.py
"""
import os
import sys

os.environ.setdefault("AUTOTRADER_TEST_LOG", "1")   # core/main 임포트보다 먼저

import inspect
import math
import time as _t
from datetime import datetime, timedelta, time as dtime

import core.strategy_manager as SM                      # noqa: E402
import core.slot_replacement as SR                      # noqa: E402
from core.phase1b_controller import Phase1BController    # noqa: E402
from core.strategy.trade_flow import TradeFlowTracker    # noqa: E402

PASS, FAIL = [], []
T0 = _t.time()


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


def section(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


# ─────────────────────────────────────────────────────────
# 스텁 — 실물과 **같은 형식**으로 돌려준다.
# ⚠️ 08-05 심야 교훈: __getattr__로 전부 None을 돌려주는 스텁을 썼다가
#    매도가 하나도 안 나가는 '가짜 통과'를 만들었다. 스텁이 실물과 다르면
#    감사 자체가 거짓말을 한다.
# ─────────────────────────────────────────────────────────
class _Repo:
    rows, sells, updates = [], [], []
    @classmethod
    def find_holdings(cls): return []
    @classmethod
    def find_by_date(cls, d): return []
    @classmethod
    def insert_buy(cls, **kw): cls.rows.append(kw); return len(cls.rows)
    @classmethod
    def update_sell(cls, **kw): cls.sells.append(kw); return True
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

    def __init__(self, change_rate=3.0):
        self.change_rate = change_rate
        self.candles = None
        self.calls = []

    def get_minute_candles(self, code, interval=1, count=1, base_date=None):
        self.calls.append(("candles", code))
        if self.candles is not None:
            return list(self.candles)
        return [{"time_str": "20260810090000", "open": 9_990, "high": 10_010,
                 "low": 9_980, "close": 10_000, "volume": 1_000}] * max(1, count)

    def get_daily_candles(self, code, count=30, base_date=None): return []
    def get_orderable_amount(self): return 10_000_000
    def get_stock_change_rate(self, code): return self.change_rate
    def get_basic_quote(self, code): return {"change_rate": self.change_rate}
    def get_index_change_rate(self, s="001"): return 0.0     # 실물은 항상 float
    def get_current_price(self, code): return 10_000


class _OrderMgr:
    def __init__(self): self.orders = []

    def buy(self, code, qty, price=0, sizing="REGULAR", exit_strategy="REGULAR",
            order_style="limit", ref_price=0):
        self.orders.append({"side": "buy", "code": code, "qty": qty,
                            "style": order_style, "ref_price": ref_price})
        return {"success": True, "ord_no": "1", "price": ref_price or 10_000,
                "style": order_style}

    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"side": "sell", "code": code, "qty": qty,
                            "style": order_style, "price": price})
        return {"success": True, "ord_no": "2", "price": price or 10_000,
                "style": order_style}

    def get_stock_name(self, code): return code


def build(now_dt=datetime(2026, 8, 10, 9, 6, 0), change_rate=3.0):
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows, _Repo.sells, _Repo.updates = [], [], []
    return SM.StrategyManager(
        kiwoom_rest=_Rest(change_rate), order_manager=_OrderMgr(),
        phase1b_controller=Phase1BController(), portfolio_optimizer=None,
        now_func=lambda: now_dt,
    )


def setup(strat, code, cond="주도주상위", open_px=10_000, ask=10_000):
    strat._first_seen[code] = _t.time() - 999
    strat._cond_names[code] = cond
    strat._stock_names[code] = code
    strat.watch_list_today.add(code)
    if open_px:
        strat._opening_prices[code] = open_px
    strat.phase1b.start_watching(code)
    strat.phase1b.orderbook.update(
        code, {"ask_prices": [ask, ask + 10, ask + 20],
               "ask_volumes": [3_000, 3_000, 3_000]}, now=_t.time())


def hot(tf, code, t_end, price=10_000, n=40):
    """최근 30초에 집중 유입 -> 가속도 >= 2.5 성립."""
    for i in range(8):
        tf.add_tick(code, price, "buy", 200, now=t_end - 110 + i * 10)
    for i in range(n):
        tf.add_tick(code, price, "buy", 900, now=t_end - 25 + i * 0.6)


def arm(strat, code, t0, price=10_000):
    for dt_ in (0.0, SM.TICK_STRENGTH_SUSTAIN_SEC + 0.5):
        strat.on_trade({"stock_code": code, "price": price, "side": "buy",
                        "volume": 10, "strength": 130.0}, now=t0 + dt_)


NOW = _t.time()

# ══════════════════════════════════════════════════════════
section("[1] 진입 경로 2개가 같은 게이트를 지나는가")
# ══════════════════════════════════════════════════════════
# 이 코드베이스의 반복 사고 1위 — 틱 경로만 고치고 폴링 경로를 빼먹는 것.
src_poll = inspect.getsource(SM.StrategyManager._evaluate_1a_pullback_entry)
src_tick = inspect.getsource(SM.StrategyManager._maybe_tick_entry)
check("폴링 경로가 evaluate_tick_entry를 거친다(자체 발사 로직 없음)",
      "evaluate_tick_entry" in src_poll)
check("폴링 경로에 check_burst 직접 호출이 없다",
      "check_burst" not in src_poll)
check("틱 경로에도 check_burst 직접 호출이 없다",
      "check_burst" not in src_tick)
# 발사는 evaluate_tick_entry 한 곳에서만 판정한다.
# ⚠️ (2026-08-09) 여기는 원래 `"self.check_burst" not in src_eval`라는
# **소스 문자열 단언**이었다. 파동 카운트 결함을 고치면서 카운트 목적으로
# check_burst를 부르게 되자 **멀쩡한 코드가 실패**로 잡혔다 — 이 코드베이스가
# 반복해서 경고해 온 바로 그 함정이다(08-06 아침 오탐 3건, 같은 날 오후 1건).
# -> **동작으로** 검증한다: 발사 판정 자체가 _fire_gate에서 나오는지를
#    스파이로 확인하고, 면제 구간에서 거래대금이 0원이어도 발사되는지(=
#    check_burst가 발사를 결정하지 않는지)를 실제 호출로 본다.
src_eval = inspect.getsource(SM.StrategyManager.evaluate_tick_entry)
check("evaluate_tick_entry가 _fire_gate를 거친다", "_fire_gate" in src_eval)

_fg_calls = {"n": 0}
s1z = build(datetime(2026, 8, 10, 9, 2, 0))
setup(s1z, "ZZZ")
arm(s1z, "ZZZ", NOW - 10.0)
_orig_fg = s1z._fire_gate
def _spy_fg(*a, **kw):
    _fg_calls["n"] += 1
    return _orig_fg(*a, **kw)
s1z._fire_gate = _spy_fg
okz, dz = s1z.evaluate_tick_entry("ZZZ", "1A", 10_000, open_price=10_000,
                                  cond_name="주도주상위", now=NOW,
                                  now_dt=datetime(2026, 8, 10, 9, 2, 0))
check("발사 판정이 _fire_gate에서 나온다(스파이 호출 확인)", _fg_calls["n"] == 1,
      f"호출 {_fg_calls['n']}회")
# 틱 버퍼가 완전히 빈 종목 = check_burst는 반드시 False. 그런데도 발사돼야
# '면제가 진짜 면제'다 — 즉 발사를 결정하는 것은 check_burst가 아니다.
_cb_ok, _ = s1z.check_burst("ZZZ", now=NOW,
                            now_dt=datetime(2026, 8, 10, 9, 2, 0))
check("면제 구간: check_burst가 False여도 발사된다(발사≠check_burst)",
      _cb_ok is False and okz is True, f"check_burst={_cb_ok} 발사={okz}")

# 실제 호출로 두 경로가 같은 결론을 내는지 확인 (09:06, 가속 미달)
_fired = {"n": 0}
_orig_fg = SM.StrategyManager._fire_gate
try:
    SM.StrategyManager._fire_gate = lambda self, *a, **k: (
        _fired.__setitem__("n", _fired["n"] + 1), (False, {"reason": "감사스텁"}))[1]
    s1 = build(datetime(2026, 8, 10, 9, 6, 0))
    setup(s1, "P1")
    arm(s1, "P1", NOW - 10)
    s1.evaluate_tick_entry("P1", "1A", 10_000, open_price=10_000,
                           cond_name="주도주상위", now=NOW,
                           now_dt=datetime(2026, 8, 10, 9, 6, 0))
    n_tick = _fired["n"]
    s1._evaluate_1a_pullback_entry(
        "P1", "P1", 1, s1.api.get_minute_candles("P1", count=20), 10_000, 10_000,
        dtime(9, 6, 0))
    n_poll = _fired["n"] - n_tick
finally:
    SM.StrategyManager._fire_gate = _orig_fg
check("틱 경로가 발사 게이트를 호출한다", n_tick >= 1, f"{n_tick}회")
check("폴링 경로도 같은 발사 게이트를 호출한다", n_poll >= 1, f"{n_poll}회")

# ══════════════════════════════════════════════════════════
section("[2] 매수를 '만드는' 경로 전수 — 게이트를 우회할 수 없는가")
# ══════════════════════════════════════════════════════════
buy_callers = [n for n in dir(SM.StrategyManager)
               if not n.startswith("__") and n != "_execute_buy"   # 자기 자신 제외
               and inspect.isfunction(getattr(SM.StrategyManager, n, None))
               and "_execute_buy(" in inspect.getsource(getattr(SM.StrategyManager, n))]
# ⚠️ 문서는 오래 "3곳"이라 적었지만 **실제로는 4곳**이다 — `_maybe_tick_entry`에
#    `ENTRY_PULLBACK_ENABLED=False`일 때 쓰는 즉시매수 폴백이 하나 더 있다
#    (08-08 심층 감사에서 발견, 문서 수정함). 목록이 틀리면 "규칙을 모든
#    매수 경로에 반영했는가"라는 이 프로젝트의 핵심 점검이 통째로 새어나간다.
check("_execute_buy 호출부는 정확히 4곳",
      len(buy_callers) == 4, ", ".join(sorted(buy_callers)))
check("호출부 구성이 문서와 같다(폴링/틱폴백/되돌림체결/추가매수)",
      set(buy_callers) == {"_evaluate_1a_pullback_entry", "_maybe_tick_entry",
                           "_try_fill_entry_plan", "_do_rescue_add"},
      str(sorted(buy_callers)))
# 그 4번째(폴백)는 되돌림이 켜져 있으면 도달 불가여야 한다
_src_mte = inspect.getsource(SM.StrategyManager._maybe_tick_entry)
check("틱 폴백 매수는 되돌림이 꺼졌을 때만 도달한다(현재는 사실상 죽은 경로)",
      "if ENTRY_PULLBACK_ENABLED and ENTRY_PULLBACK_TRANCHES:" in _src_mte
      and SM.ENTRY_PULLBACK_ENABLED and bool(SM.ENTRY_PULLBACK_TRANCHES))

src_exec = inspect.getsource(SM.StrategyManager._execute_buy)
for label, token in (("15:10 하드컷오프", "ENTRY_HARD_CUTOFF"),
                     ("등락률 게이트", "_entry_change_reject"),
                     ("전면차단", "_entry_block_reason"),
                     ("pending 회수", "finally")):
    check(f"_execute_buy 최종 가드에 {label} 존재", token in src_exec)

# 초반 캡은 can_buy_more 단일 창구여야 한다
cap_callers = [n for n in dir(SM.StrategyManager)
               if inspect.isfunction(getattr(SM.StrategyManager, n, None))
               and "early_slot_cap_reject(" in inspect.getsource(getattr(SM.StrategyManager, n))
               and n != "early_slot_cap_reject"]
check("초반 캡 판정 호출부가 can_buy_more + 진단 2곳뿐",
      set(cap_callers) <= {"can_buy_more", "_maybe_tick_entry"},
      str(sorted(cap_callers)))
check("can_buy_more가 초반 캡을 본다", "early_slot_cap_reject" in
      inspect.getsource(SM.StrategyManager.can_buy_more))

# 되돌림 체결(2차 트랜치)은 캡 재확인을 하지 않아야 한다 —
# 이미 슬롯을 점유한 종목이라 재확인하면 반쪽 포지션이 된다.
check("되돌림 체결 경로는 초반 캡을 재확인하지 않는다(반쪽 포지션 방지)",
      "early_slot_cap_reject" not in
      inspect.getsource(SM.StrategyManager._try_fill_entry_plan))

# ══════════════════════════════════════════════════════════
section("[3] 매도가 잘못 나가지 않는가 — 청산 무영향 실증")
# ══════════════════════════════════════════════════════════
src_price = inspect.getsource(SM.StrategyManager.on_price_update)
for token, label in (("_fire_gate", "발사 게이트"),
                     ("early_slot_cap_reject", "초반 슬롯 캡"),
                     ("FIRE_ACCEL_MIN", "가속 문턱"),
                     ("EARLY_SLOT_CAP", "캡 상수")):
    check(f"청산 판정(on_price_update)이 {label}을 보지 않는다", token not in src_price)

# 실제로 손절/익절이 그대로 나가는지 — 시각을 09:02(캡·면제 구간)로 두고 확인
def _pos(s, code, buy=10_000, qty=10, warm_done=True):
    s.holdings[code] = {
        "sub_strategy": "1A", "buy_price": buy, "buy_quantity": qty,
        "quantity": qty, "stock_name": code, "buy_time": s._now() - timedelta(minutes=5),
        "warmup_until": s._now() - timedelta(seconds=1) if warm_done
        else s._now() + timedelta(seconds=99),
        "highest_price": buy, "lowest_price": buy, "entry_score": 1.0,
        "phase": 1, "trade_id": 1, "origin_price": buy,
    }
    s.phase1b.start_watching(code)

s3 = build(datetime(2026, 8, 10, 9, 2, 0))
_pos(s3, "SL")
s3.on_price_update("SL", 9_600)          # -4%
sells = [o for o in s3.order_manager.orders if o["side"] == "sell"]
check("초반 구간에서도 손절(-3% 초과)은 정상 집행", len(sells) == 1,
      f"매도 {len(sells)}건")
check("손절은 시장가", sells and sells[0]["style"] == "market")

s3b = build(datetime(2026, 8, 10, 9, 2, 0))
_pos(s3b, "TP")
s3b.on_price_update("TP", 10_600)        # +6% (개장초반 캡 2.5%)
check("초반 구간에서도 익절 캡은 정상 집행",
      len([o for o in s3b.order_manager.orders if o["side"] == "sell"]) >= 1)

s3c = build(datetime(2026, 8, 10, 9, 2, 0))
_pos(s3c, "OK")
s3c.on_price_update("OK", 10_050)        # +0.5% — 아무 규칙도 성립 안 함
check("정상 구간에선 매도가 나가지 않는다(오매도 없음)",
      not [o for o in s3c.order_manager.orders if o["side"] == "sell"])

# 15:10 강제청산 — ⚠️ check_timeouts가 아니라 **main.task_force_close_watcher**가
# 담당한다(보유 전체를 순회하며 _execute_sell). 여기서는 그 배선과, 실제
# _execute_sell이 시장가로 나가는지를 확인한다.
import main as _main                                        # noqa: E402
_src_fc = inspect.getsource(_main.TradingBot.task_force_close_watcher)
check("강제청산 태스크가 FORCE_CLOSE_TIME을 본다", "FORCE_CLOSE_TIME" in _src_fc)
check("강제청산 태스크가 holdings 전체를 순회한다",
      "strategy_mgr.holdings" in _src_fc)
check("강제청산 태스크가 _execute_sell을 부른다", "_execute_sell" in _src_fc)
s3d = build(datetime(2026, 8, 10, 15, 11, 0))
_pos(s3d, "FC")
s3d._execute_sell("FC", 10_000, "장마감 강제청산")
_fc = [o for o in s3d.order_manager.orders if o["side"] == "sell"]
check("강제청산 매도가 시장가로 나간다", len(_fc) == 1 and _fc[0]["style"] == "market",
      str(_fc))
check("청산 후 보유에서 제거된다", "FC" not in s3d.holdings)

# ══════════════════════════════════════════════════════════
section("[4] '팔았는데 못 사는' 반쪽 동작 (08-03 -235,860원 사고 재발 방지)")
# ══════════════════════════════════════════════════════════
# 장중 재시작으로 6종목이 복원된 채 09:02를 지나는 상황.
s4 = build(datetime(2026, 8, 10, 9, 2, 0))
for i in range(SM.MAX_HOLDINGS):
    _pos(s4, f"H{i}")
    s4.holdings[f"H{i}"]["buy_time"] = s4._now() - timedelta(minutes=30)
before = len([o for o in s4.order_manager.orders if o["side"] == "sell"])
cnt = SR.try_slot_replacement(s4, None, 0, s4._now())
after = len([o for o in s4.order_manager.orders if o["side"] == "sell"])
check("초반 캡 중엔 슬롯 교체가 매도하지 않는다(반쪽 동작 차단)",
      after == before and cnt == 0, f"매도 {after - before}건, count={cnt}")
check("그 시점에 실제로 캡이 매수를 막고 있다(전제 확인)",
      s4.early_slot_cap_reject() is not None and s4.can_buy_more({}, "1A") is False)

# 대조군 — 09:06(캡 해제)에는 기존대로 동작 가능해야 한다
s4b = build(datetime(2026, 8, 10, 9, 6, 0))
for i in range(SM.MAX_HOLDINGS):
    _pos(s4b, f"H{i}")
check("[대조군] 09:06엔 캡이 교체를 막지 않는다",
      s4b.early_slot_cap_reject() is None)

# 우선순위 교체도 같은 방어 — 전면차단 중엔 팔지 않는다
s4c = build(datetime(2026, 8, 10, 9, 6, 0))
_pos(s4c, "H0")
s4c.quarantine_until = s4c._now() + timedelta(minutes=5)   # WS 격리
sold_before = len([o for o in s4c.order_manager.orders if o["side"] == "sell"])
s4c._try_1a_priority_upgrade("CAND", 99.0)
check("전면차단(WS격리) 중엔 우선순위 교체가 매도하지 않는다",
      len([o for o in s4c.order_manager.orders if o["side"] == "sell"]) == sold_before)

# 🔴 (2026-08-09 추가) 초반 캡 중 우선순위 교체는 **막지 않는다** — 대신
# 반쪽이 되지 않아야 한다. `try_slot_replacement`와 다른 이유:
#   · slot_replacement는 팔기만 하고 매수는 일반 진입 경로에 맡긴다 -> 캡에
#     걸려 있으면 매수가 안 와서 반쪽이 된다(그래서 위에서 막았다).
#   · priority_upgrade는 호출부가 매도 직후 can_buy를 **다시 확인해 바로 산다**.
#     `_execute_sell`이 holdings를 동기적으로 지우므로 점유가 4->3이 되어
#     캡이 풀린다. 즉 여기서 캡을 막으면 오히려 교체 기능이 통째로 죽는다.
# 캡 도입으로 이 경로가 09:00~09:05에도 열렸으므로(예전엔 6칸 만석이라 4일
# 1회) 실제로 완결되는지 못박아 둔다.
s4d = build(datetime(2026, 8, 10, 9, 2, 0))
for i in range(SM.EARLY_SLOT_CAP):
    _pos(s4d, f"C{i}")
    s4d.holdings[f"C{i}"]["buy_time"] = s4d._now() - timedelta(minutes=30)
    s4d.holdings[f"C{i}"]["entry_score"] = 1.0
    s4d.phase1b.start_watching(f"C{i}")
    s4d.phase1b.trade_flow.add_tick(f"C{i}", 10_000, "buy", 10, now=NOW - 1)
_info = {"score": 10.0, "score_threshold": 3.0}
check("[전제] 캡이 실제로 매수를 막고 있다",
      s4d.early_slot_cap_reject() is not None
      and s4d.can_buy_more(_info, "1A") is False)
_s_before = len([o for o in s4d.order_manager.orders if o["side"] == "sell"])
# (2026-08-10) 교체 상수를 0(OFF)으로 내렸으므로 이 블록에서만 잠시 되살린다.
# 여기서 검증하는 건 '한도'가 아니라 **초반 캡 중에도 반쪽 동작이 아닌가**라는
# 배선이다. 되살리는 날 이 검증이 없으면 그대로 사고가 난다.
_sv_prio = SM.PHASE1A_PRIORITY_MAX_PER_DAY
SM.PHASE1A_PRIORITY_MAX_PER_DAY = 6
try:
    _upgraded = s4d._try_1a_priority_upgrade("CAND", 99.0)
finally:
    SM.PHASE1A_PRIORITY_MAX_PER_DAY = _sv_prio
_s_after = len([o for o in s4d.order_manager.orders if o["side"] == "sell"])
check("초반 캡 중 우선순위 교체가 정확히 1건만 매도한다",
      _upgraded is True and _s_after - _s_before == 1,
      f"매도 {_s_after - _s_before}건")
check("🔴 매도 직후 자리가 비어 매수가 열린다(반쪽 동작 아님)",
      s4d.occupied_slots() == SM.EARLY_SLOT_CAP - 1
      and s4d.can_buy_more(_info, "1A") is True,
      f"점유 {s4d.occupied_slots()}")

# ══════════════════════════════════════════════════════════
section("[5] 우선순위 교체 폭주 한도 (초반 캡의 부작용)")
# ══════════════════════════════════════════════════════════
check("일일 한도 상수 존재", hasattr(SM, "PHASE1A_PRIORITY_MAX_PER_DAY"))
check("한도 <= 슬롯 수 (슬롯당 평균 1회)",
      SM.PHASE1A_PRIORITY_MAX_PER_DAY <= SM.MAX_HOLDINGS,
      f"{SM.PHASE1A_PRIORITY_MAX_PER_DAY} <= {SM.MAX_HOLDINGS}")
check("카운터가 인스턴스에 있다", hasattr(build(), "_priority_upgrades_today"))

s5 = build(datetime(2026, 8, 10, 9, 2, 0))
for i in range(SM.EARLY_SLOT_CAP):
    _pos(s5, f"W{i}")
    s5.holdings[f"W{i}"]["buy_time"] = s5._now() - timedelta(seconds=120)
    s5.phase1b.trade_flow.add_tick(f"W{i}", 10_000, "buy", 1, now=NOW)
n_sold = 0
for i in range(SM.PHASE1A_PRIORITY_MAX_PER_DAY + 4):
    before = len([o for o in s5.order_manager.orders if o["side"] == "sell"])
    s5._try_1a_priority_upgrade(f"C{i}", 999.0)
    after = len([o for o in s5.order_manager.orders if o["side"] == "sell"])
    if after > before:
        n_sold += 1
        _pos(s5, f"R{i}")   # 판 자리를 다시 채워 다음 시도가 가능하게
        s5.holdings[f"R{i}"]["buy_time"] = s5._now() - timedelta(seconds=120)
        s5.phase1b.trade_flow.add_tick(f"R{i}", 10_000, "buy", 1, now=NOW)
check(f"교체가 일일 한도({SM.PHASE1A_PRIORITY_MAX_PER_DAY})에서 멈춘다",
      n_sold <= SM.PHASE1A_PRIORITY_MAX_PER_DAY, f"실제 {n_sold}회")
check("한도 소진 후엔 추가 매도가 없다",
      s5._priority_upgrades_today <= SM.PHASE1A_PRIORITY_MAX_PER_DAY,
      str(s5._priority_upgrades_today))

# ══════════════════════════════════════════════════════════
section("[6] 수치 정합성 — 문서 / 코드 / 재계산이 세 곳 다 같은가")
# ══════════════════════════════════════════════════════════
doc = open("CLAUDE.md", encoding="utf-8").read()
for label, s_ in (
    ("발사분리", f"발사분리 {SM.FIRE_GATE_SPLIT_ENABLED} {SM.FIRE_GATE_ACCEL_FROM} "
                f"{SM.FIRE_ACCEL_MIN} {SM.FIRE_ACCEL_SHORT_SEC} "
                f"{SM.FIRE_ACCEL_LONG_SEC} {SM.FIRE_ACCEL_MIN_TICKS}"),
    ("초반캡", f"초반캡 {SM.EARLY_SLOT_CAP_ENABLED} {SM.EARLY_SLOT_CAP_UNTIL} "
              f"{SM.EARLY_SLOT_CAP}"),
):
    check(f"문서 기대값 블록에 {label} 일치", s_ in doc, s_)

# 가속도 상한 = LONG/SHORT 를 실제 계산으로 재확인
tf = TradeFlowTracker()
for i in range(30):
    tf.add_tick("MX", 10_000, "buy", 10, now=NOW - 20 + i * 0.5)
a = tf.value_acceleration("MX", short_sec=SM.FIRE_ACCEL_SHORT_SEC,
                          long_sec=SM.FIRE_ACCEL_LONG_SEC, now=NOW)
cap_math = SM.FIRE_ACCEL_LONG_SEC / SM.FIRE_ACCEL_SHORT_SEC
check(f"가속도가 수학적 상한({cap_math})을 넘지 않는다(실측)",
      a <= cap_math + 1e-9, f"{a:.4f} <= {cap_math}")
check("요구 문턱이 상한 미만 — 도달 가능", SM.FIRE_ACCEL_MIN < cap_math)

# 워밍업 아티팩트 공식 재현: accel = 4*min(30,t)/t
# 이론값 accel = 4*min(30,t)/t 를 t=40초(09:00:40)와 t=60초(09:01)에서 확인.
# t<=48초면 문턱 2.5를 넘고, 09:01(t=60)엔 2.0으로 떨어져 못 넘는다.
for t_el, must_pass in ((40, True), (60, False)):
    tf2 = TradeFlowTracker()
    for i in range(t_el * 2):
        tf2.add_tick("WU", 10_000, "buy", 10, now=NOW - t_el + i * 0.5)
    a_wu = tf2.value_acceleration("WU", short_sec=30, long_sec=120, now=NOW)
    expect = 4 * min(30, t_el) / t_el
    check(f"워밍업 공식 4*min(30,t)/t 재현 (t={t_el}초)",
          abs(a_wu - expect) < 0.2, f"실측 {a_wu:.2f} vs 이론 {expect:.2f}")
    got = a_wu >= SM.FIRE_ACCEL_MIN
    check(f"t={t_el}초에서 문턱 {SM.FIRE_ACCEL_MIN} {'통과' if must_pass else '탈락'}",
          got is must_pass, f"accel={a_wu:.2f}")
check("-> 균일 거래만으로 문턱을 넘는 한계 시각 = 120/문턱 초",
      abs(SM.FIRE_ACCEL_LONG_SEC / SM.FIRE_ACCEL_MIN - 48.0) < 1e-6,
      f"{SM.FIRE_ACCEL_LONG_SEC / SM.FIRE_ACCEL_MIN:.1f}초")

# 버퍼 창과 LONG 일치 (조용한 절단 방지)
check("FIRE_ACCEL_LONG_SEC == 라이브 트래커 버퍼창",
      SM.FIRE_ACCEL_LONG_SEC == build().phase1b.trade_flow.max_window_sec)

# 진입 게이트 상수 전수 — 문서 표와 대조
expect_map = {
    "TICK_STRENGTH_MIN": 100.0, "TICK_STRENGTH_SUSTAIN_SEC": 3.0,
    "MIN_ENTRY_DELAY_SEC": 60.0, "MIN_ENTRY_CHANGE_PCT": 0.0,
    "BURST_WAVE_MAX": 3, "VWAP_ENTRY_MIN_GAP_PCT": 0.5,
    "PHASE1A_MAX_SLOTS": 8, "PULLBACK_MAX_SLOTS": 0, "MAX_HOLDINGS": 6,
    "FIRE_ACCEL_MIN": 2.5, "FIRE_ACCEL_MIN_TICKS": 20, "EARLY_SLOT_CAP": 4,
}
bad = [f"{k}={getattr(SM, k)}(기대 {v})" for k, v in expect_map.items()
       if getattr(SM, k) != v]
check("진입 게이트 상수 12종 전수 일치", not bad, "; ".join(bad))

# ══════════════════════════════════════════════════════════
section("[7] 종일 시나리오 — 09:00~15:10을 실제 코드로 관통")
# ══════════════════════════════════════════════════════════
timeline = [
    (dtime(9, 1, 0), "초반: 면제 발사 + 캡"),
    (dtime(9, 4, 59), "초반 경계 직전"),
    (dtime(9, 5, 0), "가속 게이트 전환 + 캡 해제"),
    (dtime(9, 30, 0), "오전"),
    (dtime(12, 0, 0), "점심"),
    (dtime(14, 49, 0), "진입 종료 직전"),
    (dtime(14, 51, 0), "진입 종료 후"),
]
results = []
for tt, label in timeline:
    now_dt = datetime(2026, 8, 10, tt.hour, tt.minute, tt.second)
    s = build(now_dt)
    setup(s, "DAY")
    hot(s.phase1b.trade_flow, "DAY", NOW)
    arm(s, "DAY", NOW - 10)
    ok, info = s.evaluate_tick_entry("DAY", "1A", 10_000, open_price=10_000,
                                     cond_name="주도주상위", now=NOW, now_dt=now_dt)
    cap = s.early_slot_cap_reject(now_dt)
    results.append((label, tt, ok, info.get("burst_path"), cap))
    print(f"    {tt} {label:<24} 발사={'O' if ok else 'X'} "
          f"경로={info.get('burst_path') or info.get('reason','')[:22]} "
          f"캡={'ON' if cap else 'off'}")

check("09:01 발사 경로가 '개장초반'",
      results[0][3] == "개장초반", str(results[0][3]))
check("09:04:59도 '개장초반'", results[1][3] == "개장초반")
check("09:05:00부터 '가속'", results[2][3] == "가속", str(results[2][3]))
check("09:30/12:00/14:49도 '가속'(전 구간 일관)",
      all(r[3] == "가속" for r in results[3:6]),
      str([r[3] for r in results[3:6]]))
check("초반 캡은 09:05 이전에만 켜진다",
      [bool(r[4]) for r in results] == [False] * 7 or
      all((r[1] < SM.EARLY_SLOT_CAP_UNTIL) or not r[4] for r in results))
# 진입 종료(14:50) 이후엔 틱 경로 자체가 매수하지 않는다
s7 = build(datetime(2026, 8, 10, 14, 51, 0))
setup(s7, "LATE")
hot(s7.phase1b.trade_flow, "LATE", NOW)
s7.on_trade({"stock_code": "LATE", "price": 10_000, "side": "buy",
             "volume": 10, "strength": 130.0}, now=NOW)
s7.on_trade({"stock_code": "LATE", "price": 10_000, "side": "buy",
             "volume": 10, "strength": 130.0}, now=NOW + 5)
check("14:51엔 매수도 계획 생성도 없다",
      "LATE" not in s7.holdings and "LATE" not in s7._entry_plans
      and not [o for o in s7.order_manager.orders if o["side"] == "buy"])

# ══════════════════════════════════════════════════════════
section("[8] 이상 입력 내성 — 쓰레기가 들어와도 매수/매도가 새지 않는가")
# ══════════════════════════════════════════════════════════
bad_inputs = [0, None, -1, "", "abc", -9_800, 1, 10_000_000, float("nan")]
s8 = build(datetime(2026, 8, 10, 9, 2, 0))
_pos(s8, "BAD")
err = 0
for v in bad_inputs:
    try:
        s8.on_price_update("BAD", v)
    except Exception:
        err += 1
sells8 = [o for o in s8.order_manager.orders if o["side"] == "sell"]
check("이상 가격 9종에 예외 0건", err == 0, f"예외 {err}건")
check("이상 가격으로 매도가 나가지 않는다", not sells8, f"매도 {len(sells8)}건")

# ── (2026-08-09 추가) `_execute_sell` 직접 호출 경로의 기록가 위생 ────────
# 08-05에 "**모든 매도가 on_price_update를 지난다**"고 적었지만 사실이 아니다 —
# check_timeouts / try_slot_replacement / _try_1a_priority_upgrade /
# main.task_force_close_watcher는 `_execute_sell`을 **직접** 부른다.
# 그 경로들은 가격 조건 없이 청산하므로 잘못된 매도가 나가지는 않지만,
# current_price가 오염되면 기록 손익이 틀어지고 그 값이 `_daily_realized`로
# 흘러 **MDD 일손실 차단(-3%)을 잘못 트립**시켜 그날 매수를 전면 차단할 수 있다.
# 요구사항은 두 가지이고 **순서가 중요하다**:
#   ① 매도는 무슨 값이 와도 반드시 나간다(막으면 보유분이 무방비가 된다)
#   ② 기록 손익은 오염되지 않는다
for v in bad_inputs:
    s8s = build(datetime(2026, 8, 10, 10, 0, 0))
    _pos(s8s, "SELLBAD")
    _bp = s8s.holdings["SELLBAD"]["buy_price"]
    try:
        s8s._execute_sell("SELLBAD", v, "직접호출 강제청산")
        _exc = False
    except Exception:
        _exc = True
    _sold = [o for o in s8s.order_manager.orders if o["side"] == "sell"]
    check(f"[{v!r}] 이상 기록가에도 매도는 반드시 나간다",
          (not _exc) and len(_sold) == 1 and "SELLBAD" not in s8s.holdings,
          f"예외={_exc} 매도={len(_sold)} 보유잔존={len(s8s.holdings)}")
    # 기록가는 직접 못 보므로 '손익이 비상식적이지 않은가'로 확인한다.
    # 정화가 동작하면 최악이라도 buy_price(손익 0)로 수렴한다.
    check(f"[{v!r}] 기록 손익이 오염되지 않는다(|일실현| <= 매수금액)",
          abs(s8s._daily_realized) <= _bp * 10 * 1.5,
          f"일실현 {s8s._daily_realized:,.0f}")

# 대조군 — 정상 가격이면 정화가 개입하지 않고 손익이 그대로 기록된다
s8n = build(datetime(2026, 8, 10, 10, 0, 0))
_pos(s8n, "SELLOK")
s8n._execute_sell("SELLOK", 10_500, "직접호출 정상청산")
check("[대조군] 정상 가격은 정화하지 않는다(이익이 기록된다)",
      s8n._daily_realized > 0, f"일실현 {s8n._daily_realized:,.0f}")

# 발사 게이트도 같은 내성 — 틱/데이터가 망가져도 매수를 만들지 않는다
s8b = build(datetime(2026, 8, 10, 9, 6, 0))
setup(s8b, "GB")
errs = 0
for v in (0, -5, float("nan"), 1e12):
    try:
        s8b.phase1b.trade_flow.add_tick("GB", v, "buy", 10, now=NOW)
    except Exception:
        errs += 1
try:
    ok8, d8 = s8b._fire_gate("GB", now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0))
except Exception:
    ok8, d8, errs = None, {}, errs + 1
check("이상 틱에도 발사 판정이 예외 없이 끝난다", errs == 0, f"예외 {errs}건")
check("이상 틱만으로는 발사되지 않는다(틱수 가드)", ok8 is False,
      str(d8.get("reason"))[:60])

# 시각을 모르는 경우(now_dt=None)에도 안전한가
s8c = build(datetime(2026, 8, 10, 9, 2, 0))
setup(s8c, "TN")
ok8c, _ = s8c._fire_gate("TN", now=NOW)      # now_dt 생략 -> self._now() 사용
check("now_dt 생략 시 self._now()로 판정(예외 없음)", isinstance(ok8c, bool))

# 캡 판정도 now_dt 생략 안전
check("초반 캡도 now_dt 생략 안전",
      s8c.early_slot_cap_reject() is None or isinstance(s8c.early_slot_cap_reject(), str))

# ══════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건   ({_t.time() - T0:.1f}초)")
if FAIL:
    for f in FAIL:
        print(f"  - {f}")
print("=" * 70)
sys.exit(1 if FAIL else 0)
