"""2026-08-04 패치 격리 검증 — 되돌림 대기 분할매수 + 분할매도.

배경 (전부 08-03/08-04 틱 아카이브 실측에서 나왔다):
  ① 버스트 트리거 시점은 **국소 고점**이다. 측정한 6건 전부가 트리거 후 60초
     안에 되돌림을 겪었다(-0.45% ~ -1.78%). 버스트는 정의상 "그 순간 열기가
     가장 뜨거운 지점"이라 거기서 사면 구조적으로 가장 비싸게 산다.
     -> 트리거 즉시 사지 않고 -0.5%/-1.0% 2단으로 나눠 되돌림을 기다린다.
     -0.5%를 1차로 쓰는 근거: 6건 **전부 체결**(스킵 0)인데 진입가만 평균
     +0.74% 개선 — 표본이 안 바뀌므로 선택 편향이 없는 순수 개선이다.
  ② 체결강도 하락 신호는 **+1분 예측력만** 있다(6건 전부 음수, 평균 -0.91%).
     +3분/+5분은 부호가 갈린다(037070 +6.5%). 그런데 기존 코드는 이 1분짜리
     신호로 포지션을 100% 영구청산했다 — 시간지평 불일치다.
     -> 신호 시 50%만 팔고, 잔량은 고점 대비 3% 트레일에 맡긴다.
     트레일 3%인 근거: '고점 도달 전 되돌림'이 매드업 2.03% / 037070 3.90%였다.

네트워크·DB·키움 API를 타지 않는 순수 격리 테스트.
실행: python test_patch_20260804.py   (종료코드 0 = 전원 통과)
"""
import sys
import time
from datetime import datetime, timedelta

import os as _os_testlog
# 실거래 로그(autotrader.log) 오염 방지 — 반드시 core/main 임포트보다 먼저.
_os_testlog.environ["AUTOTRADER_TEST_LOG"] = "1"

import core.strategy_manager as SM
from core.phase1b_controller import Phase1BController

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


# ─────────────────────────────────────────────────────────
# 스텁 (test_patch_20260801/02와 동일 계약)
# ─────────────────────────────────────────────────────────
class _Repo:
    rows = []
    sells = []
    updates = []
    @classmethod
    def find_holdings(cls): return []
    @classmethod
    def find_by_date(cls, d): return []
    @classmethod
    def insert_buy(cls, **kw): cls.rows.append(kw); return len(cls.rows)
    @classmethod
    def update_sell(cls, **kw): cls.sells.append(kw); return True
    @classmethod
    def update(cls, row_id, data): cls.updates.append({"id": row_id, **data}); return True
    @classmethod
    def add(cls, **kw): cls.rows.append(kw); return len(cls.rows)
    @classmethod
    def mark_bought(cls, i): return True
    @classmethod
    def log(cls, *a, **kw): return True


class _Theme:
    def __init__(self, *a, **kw): self.code_to_theme = {}; self.leading_themes = []
    def fetch_themes_from_github(self): pass
    def start_auto_update(self, *a, **kw): pass
    def is_leading_theme_stock(self, code): return False


class _Rest:
    host = "https://mock"
    def __init__(self): self.calls = []
    def get_minute_candles(self, code, interval=1, count=1, base_date=None):
        self.calls.append(("candles", code, count))
        return [{"time_str": "20260804090000", "open": 9_990, "high": 10_010,
                 "low": 9_980, "close": 10_000, "volume": 1_000}] * max(1, count)
    def get_orderable_amount(self): return 10_000_000
    def get_stock_change_rate(self, code): return 3.0
    def get_index_change_rate(self, s="001"): return 0.0
    def get_current_price(self, code): return 10_000


class _OrderMgr:
    """ref_price(=매도1호가)를 그대로 체결가로 돌려준다 — 실제와 같은 관례."""
    def __init__(self): self.orders = []
    def buy(self, code, qty, price=0, sizing="REGULAR", exit_strategy="REGULAR",
            order_style="limit", ref_price=0):
        self.orders.append({"code": code, "qty": qty, "style": order_style,
                            "ref_price": ref_price, "side": "buy"})
        return {"success": True, "ord_no": "1", "price": ref_price or 10_000,
                "style": order_style}
    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"code": code, "qty": qty, "style": order_style,
                            "side": "sell"})
        return {"success": True, "ord_no": "2", "price": price, "style": order_style}
    def get_stock_name(self, code): return code


def build(now_dt=datetime(2026, 8, 4, 9, 30, 0)):
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows, _Repo.sells, _Repo.updates = [], [], []
    return SM.StrategyManager(
        kiwoom_rest=_Rest(), order_manager=_OrderMgr(),
        phase1b_controller=Phase1BController(), portfolio_optimizer=None,
        now_func=lambda: now_dt,
    )


def setup(strat, code, cond="주도주상위", ask=10_000):
    strat._cond_names[code] = cond
    strat._stock_names[code] = code
    strat.watch_list_today.add(code)
    strat._opening_prices[code] = 10_000
    strat.phase1b.start_watching(code)
    strat.phase1b.orderbook.update(
        code, {"ask_prices": [ask, ask + 10, ask + 20],
               "ask_volumes": [3_000, 3_000, 3_000]}, now=time.time())


def feed(tf, code, n, value_each, now, span=0.5):
    vol = max(1, int(value_each // 10_000))
    for i in range(n):
        tf.add_tick(code, 10_000, "buy", vol, now=now - span * (i / max(1, n)))


def tick(strat, code, strength, at, price=10_000, side="buy", volume=10):
    strat.on_trade({"stock_code": code, "price": price, "side": side,
                    "volume": volume, "strength": strength}, now=at)


def trigger(strat, code, t0):
    """무장(1.5초) + 버스트 -> 되돌림 대기 계획 생성까지."""
    tick(strat, code, 130.0, t0)
    feed(strat.phase1b.trade_flow, code, 2, SM.PHASE1A_BURST_TRADE_VALUE, now=t0 + 3.5)
    tick(strat, code, 130.0, t0 + 3.5)


T = time.time()

# ═════════════════════════════════════════════════════════
print("\n[1] 상수 — 실측 근거대로 설정됐는지")
# ═════════════════════════════════════════════════════════
check("되돌림 대기 활성", SM.ENTRY_PULLBACK_ENABLED is True)
check("2단 분할 (사용자 지정: 절반씩)", len(SM.ENTRY_PULLBACK_TRANCHES) == 2)
check("1차 -0.5% (스킵 0건이었던 값)",
      abs(SM.ENTRY_PULLBACK_TRANCHES[0][0] - 0.005) < 1e-9)
check("2차 -1.0%", abs(SM.ENTRY_PULLBACK_TRANCHES[1][0] - 0.010) < 1e-9)
check("비중 합이 1.0", abs(sum(f for _, f in SM.ENTRY_PULLBACK_TRANCHES) - 1.0) < 1e-9)
check("1차가 2차보다 얕다(순서 뒤집히면 2차가 먼저 체결됨)",
      SM.ENTRY_PULLBACK_TRANCHES[0][0] < SM.ENTRY_PULLBACK_TRANCHES[1][0])
check("대기 시간 상한 존재", SM.ENTRY_PULLBACK_TIMEOUT_SEC > 0)
check("분할매도 활성", SM.PARTIAL_EXIT_ENABLED is True)
check("분할매도 비중 50%", abs(SM.PARTIAL_EXIT_FRACTION - 0.5) < 1e-9)
check("잔량 트레일 3%", abs(SM.PARTIAL_EXIT_TRAIL - 0.030) < 1e-9)
# 실측 흔들림(037070 3.90%)보다 넓어야 본 상승 전에 안 털린다.
check("트레일이 왕복수수료보다 충분히 넓다",
      SM.PARTIAL_EXIT_TRAIL > SM.ROUND_TRIP_COST * 3)

# ═════════════════════════════════════════════════════════
print("\n[2] 트리거 -> 되돌림 대기 (즉시매수 아님)")
# ═════════════════════════════════════════════════════════
s = build()
setup(s, "A1")
trigger(s, "A1", T)
check("무장+버스트 -> 계획 생성", "A1" in s._entry_plans)
check("아직 매수하지 않음(트리거는 국소 고점)", "A1" not in s.holdings)
check("대기 중에도 슬롯 점유 — 되돌림 왔을 때 살 자리 확보",
      s.occupied_slots() == 1, str(s.occupied_slots()))
plan = s._entry_plans["A1"]
check("목표가가 트리거가 대비 -0.5%/-1.0%",
      abs(plan["targets"][0]["price"] - 9_950) < 1 and
      abs(plan["targets"][1]["price"] - 9_900) < 1,
      f'{plan["targets"][0]["price"]:.0f}/{plan["targets"][1]["price"]:.0f}')

# 되돌림이 얕으면(-0.3%) 아직 안 산다
tick(s, "A1", 128.0, T + 4.0, price=9_970, side="sell")
check("-0.3%로는 체결되지 않음", "A1" not in s.holdings)

# ═════════════════════════════════════════════════════════
print("\n[3] 1차 트랜치 (-0.5%) 체결")
# ═════════════════════════════════════════════════════════
s.phase1b.orderbook.update("A1", {"ask_prices": [9_950, 9_960, 9_970],
                                  "ask_volumes": [3_000, 3_000, 3_000]})
tick(s, "A1", 128.0, T + 5.0, price=9_950, side="sell")
check("-0.5% 도달 -> 1차 체결", "A1" in s.holdings)
check("tranches_filled=1", s.holdings["A1"].get("tranches_filled") == 1)
check("계획은 아직 살아있음(2차 대기)", "A1" in s._entry_plans)
check("qty가 설정됨(부분매도 전제)", s.holdings["A1"].get("qty", 0) > 0)
q1 = s.holdings["A1"]["qty"]
avg1 = s.holdings["A1"]["buy_price"]
check("1차 진입가가 트리거가보다 낮다(개선)", avg1 < 10_000, f"{avg1:,.0f}")

# ═════════════════════════════════════════════════════════
print("\n[4] 2차 트랜치 (-1.0%) — 평단가가 낮아진다")
# ═════════════════════════════════════════════════════════
s.phase1b.orderbook.update("A1", {"ask_prices": [9_900, 9_910, 9_920],
                                  "ask_volumes": [3_000, 3_000, 3_000]})
tick(s, "A1", 126.0, T + 6.0, price=9_900, side="sell")
check("-1.0% 도달 -> 2차 체결", s.holdings["A1"].get("tranches_filled") == 2)
check("수량이 늘어남", s.holdings["A1"]["qty"] > q1,
      f'{q1} -> {s.holdings["A1"]["qty"]}')
check("평단가가 1차보다 낮아짐", s.holdings["A1"]["buy_price"] < avg1,
      f'{avg1:,.0f} -> {s.holdings["A1"]["buy_price"]:,.0f}')
check("buy_quantity도 총량으로 갱신됨",
      s.holdings["A1"]["buy_quantity"] == s.holdings["A1"]["qty"])
check("전량 체결되면 계획이 닫힘(슬롯 반환)", "A1" not in s._entry_plans)
check("DB도 평단가/수량 갱신됨(행 하나 = 한 왕복 규약 유지)",
      any("buy_price" in u and "buy_quantity" in u for u in _Repo.updates),
      str(_Repo.updates[-1]) if _Repo.updates else "없음")

# ═════════════════════════════════════════════════════════
print("\n[5] 되돌림 미도달 -> 타임아웃 회수 (슬롯 누수 방지)")
# ═════════════════════════════════════════════════════════
s2 = build()
setup(s2, "B1")
trigger(s2, "B1", T)
check("계획 생성됨", "B1" in s2._entry_plans)
check("슬롯 점유 중", s2.occupied_slots() == 1)
s2.expire_entry_plans(now=T + SM.ENTRY_PULLBACK_TIMEOUT_SEC + 10)
check("타임아웃 후 계획 회수", "B1" not in s2._entry_plans)
check("슬롯도 반환됨 (누수 없음)", s2.occupied_slots() == 0)
check("한 주도 못 샀음", "B1" not in s2.holdings)
_rej = s2._last_reject.get("B1")
check("진단에 '되돌림 미도달'이 기록됨(원인 추적 가능)",
      _rej is not None and "되돌림 미도달" in str(_rej), str(_rej))
# "기타"로 뭉개지면 장중에 원인 판단이 불가능하다 (08-02 '버스트 계산 실패' 교훈).
check("전용 카테고리로 분류됨(기타로 뭉개지지 않음)",
      _rej is not None and _rej[0] != "기타", str(_rej[0]) if _rej else "없음")
check("라벨에 수치를 박지 않음(상수 바꿔도 거짓말 안 하게)",
      _rej is not None and "0.5" not in _rej[0] and "120" not in _rej[0],
      str(_rej[0]) if _rej else "없음")

# 틱이 계속 오는 종목은 _try_fill_entry_plan 안에서도 만료된다
s2b = build()
setup(s2b, "B2")
trigger(s2b, "B2", T)
tick(s2b, "B2", 120.0, T + SM.ENTRY_PULLBACK_TIMEOUT_SEC + 10, price=9_800, side="sell")
check("만료 후 도달한 되돌림으로는 매수하지 않음", "B2" not in s2b.holdings)
check("만료된 계획도 정리됨", "B2" not in s2b._entry_plans)

# ═════════════════════════════════════════════════════════
print("\n[6] 살 수 없는 종목엔 계획을 걸지 않는다 (슬롯 낭비 방지)")
# ═════════════════════════════════════════════════════════
s3 = build()
setup(s3, "C1")
s3.api.get_stock_change_rate = lambda c: 50.0      # 전일종가대비 +50%
s3._prev_closes.pop("C1", None)
trigger(s3, "C1", T)
check("등락률 상한 초과 종목엔 계획을 걸지 않음", "C1" not in s3._entry_plans)
check("슬롯도 점유하지 않음", s3.occupied_slots() == 0, str(s3.occupied_slots()))

# ═════════════════════════════════════════════════════════
print("\n[7] 분할매도 — 신호 시 50%만 판다")
# ═════════════════════════════════════════════════════════
def put_pos(strat, code="X1", buy_price=10_000, qty=100, sub="1A", upgraded=True):
    strat.holdings[code] = {
        "trade_id": 1, "buy_price": buy_price, "buy_quantity": qty, "qty": qty,
        "buy_time": strat._now() - timedelta(minutes=10),
        "stock_name": code, "strategy_phase": 1, "sub_strategy": sub,
        "highest_price": buy_price, "lowest_price": buy_price,
        "warmup_until": strat._now() - timedelta(seconds=1),
        "tp_cap_upgraded": upgraded, "tp_cap": SM.TP_CAP_UPGRADED_MAX,
        "strength_baseline": 200.0, "tranches_filled": 1,
    }
    return strat.holdings[code]


s4 = build()
p = put_pos(s4)
# 강도 하락 + 거래량 하락 상황을 만든다 (순이익 구간)
for i in range(12):
    s4.phase1b.trade_flow.add_tick("X1", 10_300, "sell", 50, now=T - i * 0.2)
    s4.phase1b.trade_flow.add_tick("X1", 10_300, "buy", 1, now=T - i * 0.2)
s4._volume_ratio = lambda c: 0.3           # 거래량 하락
s4._update_dynamic_caps()
check("분할 1차로 절반만 매도됨", "X1" in s4.holdings,
      f"holdings={list(s4.holdings)}")
if "X1" in s4.holdings:
    check("잔량이 절반", s4.holdings["X1"]["qty"] == 50,
          str(s4.holdings["X1"]["qty"]))
    check("partial_exited 표식", s4.holdings["X1"].get("partial_exited") is True)
    check("trail_peak가 설정됨(잔량 트레일 기준)",
          s4.holdings["X1"].get("trail_peak", 0) > 0)
check("부분매도 시점엔 DB update_sell을 부르지 않음(행이 닫히면 잔량이 사라진다)",
      len(_Repo.sells) == 0, str(len(_Repo.sells)))
check("매도 주문은 실제로 나갔다(50주)",
      any(o["side"] == "sell" and o["qty"] == 50 for o in s4.order_manager.orders))

# ═════════════════════════════════════════════════════════
print("\n[8] 잔량 트레일 3% — 오르는 동안은 안 팔고, 3% 밀리면 판다")
# ═════════════════════════════════════════════════════════
if "X1" in s4.holdings:
    s4.on_price_update("X1", 10_600)        # 신고가 -> 트레일 기준 상향
    check("고점 갱신 중에는 청산하지 않음", "X1" in s4.holdings)
    peak = s4.holdings["X1"]["trail_peak"]
    check("트레일 고점이 따라 올라감", peak >= 10_600, f"{peak:,.0f}")
    s4.on_price_update("X1", int(peak * (1 - SM.PARTIAL_EXIT_TRAIL) - 1))
    check("고점 대비 3% 밀리면 잔량 청산", "X1" not in s4.holdings)
    check("완전 청산 시 DB에 1행만 기록", len(_Repo.sells) == 1, str(len(_Repo.sells)))
    if _Repo.sells:
        rec = _Repo.sells[-1]
        check("DB 수량은 분할 합산 총량(100주)",
              rec["sell_quantity"] == 100, str(rec["sell_quantity"]))
        check("DB 매도가는 수량가중 평균(두 체결가 사이)",
              10_200 < rec["sell_price"] < 10_600, f'{rec["sell_price"]:,.0f}')

# ═════════════════════════════════════════════════════════
print("\n[9] 손실반등은 전량 청산 유지 (리스크 장치는 안 쪼갠다)")
# ═════════════════════════════════════════════════════════
s5 = build()
p5 = put_pos(s5, code="Y1", upgraded=False)
p5["lowest_price"] = 9_000
p5["buy_time"] = s5._now() - timedelta(minutes=10)
for i in range(12):
    s5.phase1b.trade_flow.add_tick("Y1", 9_150, "sell", 50, now=T - i * 0.2)
    s5.phase1b.trade_flow.add_tick("Y1", 9_150, "buy", 1, now=T - i * 0.2)
s5._volume_ratio = lambda c: 0.3
s5._update_dynamic_caps()
check("손실반등은 절반이 아니라 전량 청산", "Y1" not in s5.holdings,
      f"holdings={list(s5.holdings)}")
check("손실반등 사유로 기록",
      any("손실반등" in (r.get("exit_reason") or "") for r in _Repo.sells),
      str([r.get("exit_reason") for r in _Repo.sells])[:70])

# ═════════════════════════════════════════════════════════
print("\n[10] 손절은 분할과 무관하게 전량 — 최후 방어선 불변")
# ═════════════════════════════════════════════════════════
s6 = build()
p6 = put_pos(s6, code="Z1")
p6["partial_exited"] = True
p6["qty"] = 50
p6["trail_peak"] = 10_000
s6.on_price_update("Z1", 9_600)             # -4%
check("분할 잔량도 손절은 전량 청산", "Z1" not in s6.holdings)
check("사유가 손절(트레일 아님)",
      any("손절" in (r.get("exit_reason") or "") for r in _Repo.sells),
      str([r.get("exit_reason") for r in _Repo.sells])[:70])

# ═════════════════════════════════════════════════════════
print("\n[10-b] 회귀 방지 — 폴링 경로가 되돌림 대기를 덮어쓰지 않는다")
# ═════════════════════════════════════════════════════════
# 08-04 실거래로 발견한 실제 결함: 되돌림 대기를 틱 경로에만 붙였더니
# on_condition_hit / watchlist_reentry 폴링이 _execute_buy를 직접 불러
# 계획이 열린 직후 즉시 매수해버렸다. 036930은 트리거 133,100원에 대기
# (목표 132,434)를 걸고 46초 뒤 폴링이 **133,600원**에 샀다 — 기다린 것보다
# 더 비싸게 산 셈이고 계획은 0/2 트랜치로 시간초과됐다.
s8 = build()
setup(s8, "V1")
trigger(s8, "V1", T)                     # 틱 경로가 계획을 연다
check("[사전] 계획 존재, 미매수", "V1" in s8._entry_plans and "V1" not in s8.holdings)
# 폴링 경로가 같은 종목을 평가 — 즉시 매수하면 안 된다
s8._strength_since["V1"] = T - 5.0
s8._evaluate_1a_pullback_entry(
    "V1", "V1", 1, None, 10_000, 9_800, s8._now().time())
check("폴링 경로가 대기 중 종목을 즉시 매수하지 않음", "V1" not in s8.holdings,
      f"holdings={list(s8.holdings)}")
check("계획은 그대로 유지됨(폴링이 지우지도 않음)", "V1" in s8._entry_plans)

# 틱 경로가 아직 계획을 못 연 상태에서 폴링이 먼저 와도 즉시 매수는 안 된다
s9 = build()
setup(s9, "V2")
s9._strength_since["V2"] = T - 5.0
feed(s9.phase1b.trade_flow, "V2", 2, SM.PHASE1A_BURST_TRADE_VALUE, now=T)
s9._evaluate_1a_pullback_entry(
    "V2", "V2", 1, None, 10_000, 9_800, s9._now().time())
check("폴링이 먼저 와도 즉시매수가 아니라 대기 계획을 연다",
      "V2" in s9._entry_plans and "V2" not in s9.holdings,
      f"plans={list(s9._entry_plans)} holdings={list(s9.holdings)}")
# 그 계획도 정상적으로 체결된다(경로가 달라도 진입 방식은 동일해야 한다)
s9.phase1b.orderbook.update("V2", {"ask_prices": [9_950, 9_960, 9_970],
                                   "ask_volumes": [3_000, 3_000, 3_000]})
tick(s9, "V2", 128.0, T + 5.0, price=9_950, side="sell")
check("폴링이 연 계획도 되돌림 도달 시 체결됨", "V2" in s9.holdings)

# ═════════════════════════════════════════════════════════
print("\n[11] 회귀 방지 — 청산 시 계획도 함께 정리")
# ═════════════════════════════════════════════════════════
s7 = build()
setup(s7, "W1")
trigger(s7, "W1", T)
check("계획 존재", "W1" in s7._entry_plans)
s7.reset_tick_entry_state("W1")
check("틱 상태 초기화 시 계획도 삭제(슬롯 영구점유·재매수차단 우회 방지)",
      "W1" not in s7._entry_plans)
check("슬롯 반환됨", s7.occupied_slots() == 0)

# ═════════════════════════════════════════════════════════
print("\n" + "=" * 62)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print(f"  - {f}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
