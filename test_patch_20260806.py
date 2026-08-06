"""2026-08-06 패치 격리 검증 — 08-06 장중정지의 원인 결함 [A][B] + [C][D].

08-06은 개장 18분에 매수 19건 / 손절 7건이 나와 09:19에 수동 정지한 날이다.
사후 분석에서 나온 네 가지를 여기서 못박는다.

  [A] `_today_open`이 **장중 재편입**에서 틀린 시가를 반환했다.
      09:18에 분봉 15개를 받으면 창이 09:03~09:18이라 09:00봉이 없는데
      "오늘 봉 중 가장 이른 것"을 시가로 써서 09:03봉을 당일 시가로 착각한다.
      실측(금호타이어 073240): 진짜 7,200 -> 반환 7,650. 그 결과 시가대비
      +14.03%짜리 매수가 +7.32%로 계산돼 8% 상한을 통과했다.
      두 겹이다 — 함수(A-1)와, `open_price or 캐시`라 틀린 새 값이 정확한
      캐시를 이기던 호출부(A-2).

  [B] 버스트가 체결 **방향**을 버리고 있었다(`for ts, price, _, volume in d`).
      대량 투매도 버스트로 셌고, 무장에 쓰는 FID 228은 당일 **누적**이라
      오전에 강했던 종목은 급락 중에도 100 위에 머문다.
      -> "오전에 강했던 종목이 투매로 무너지는 순간"이 매수 신호가 됐다.

  [C] 등락률 **하한**이 없어 전일 대비 마이너스에서도 샀다(주성엔지니어링
      -1.26% / -1.96%). 사용자 지정: "매수는 무조건 +일 때만."

  [D] 상승 이탈(추격매수)이 실거래에서 되돌림보다 나빴다.
      상승이탈 n=9 평균 -1.00% 승률 22% 손절 4건 vs 되돌림 n=27 +0.23% 41%.

네트워크·DB·키움 API를 타지 않는 순수 격리 테스트.
실행: python test_patch_20260806.py   (종료코드 0 = 전원 통과)
"""
import sys
import time
from datetime import datetime, timedelta

import os as _os_testlog
# 실거래 로그(autotrader.log) 오염 방지 — 반드시 core/main 임포트보다 먼저.
_os_testlog.environ["AUTOTRADER_TEST_LOG"] = "1"

import core.strategy_manager as SM
from core.phase1b_controller import Phase1BController
from core.strategy.trade_flow import TradeFlowTracker

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


# ─────────────────────────────────────────────────────────
# 스텁 (test_patch_20260804/05와 동일 계약)
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
    def update(cls, row_id, data): cls.updates.append({"id": row_id, **data}); return True
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
    """change_rate를 테스트가 조종한다 — [C] 하한 판정이 이 값에서 나온다."""
    host = "https://mock"

    def __init__(self, change_rate=3.0):
        self.calls = []
        self.change_rate = change_rate
        self.candles = None

    def get_minute_candles(self, code, interval=1, count=1, base_date=None):
        self.calls.append(("candles", code, count))
        if self.candles is not None:
            return list(self.candles)
        return [{"time_str": "20260806090000", "open": 9_990, "high": 10_010,
                 "low": 9_980, "close": 10_000, "volume": 1_000}] * max(1, count)

    def get_orderable_amount(self): return 10_000_000
    def get_stock_change_rate(self, code): return self.change_rate
    def get_basic_quote(self, code): return {"change_rate": self.change_rate}
    def get_index_change_rate(self, s="001"): return 0.0
    def get_current_price(self, code): return 10_000


class _OrderMgr:
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


def build(now_dt=datetime(2026, 8, 6, 9, 30, 0), change_rate=3.0):
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


def setup(strat, code, cond="주도주상위", ask=10_000, open_px=10_000):
    strat._cond_names[code] = cond
    strat._stock_names[code] = code
    strat.watch_list_today.add(code)
    if open_px:
        strat._opening_prices[code] = open_px
    strat.phase1b.start_watching(code)
    strat.phase1b.orderbook.update(
        code, {"ask_prices": [ask, ask + 10, ask + 20],
               "ask_volumes": [3_000, 3_000, 3_000]}, now=time.time())


def feed(tf, code, n, value_each, now, span=0.5, side="buy", price=10_000):
    vol = max(1, int(value_each // price))
    for i in range(n):
        tf.add_tick(code, price, side, vol, now=now - span * (i / max(1, n)))


def tick(strat, code, strength, at, price=10_000, side="buy", volume=10):
    strat.on_trade({"stock_code": code, "price": price, "side": side,
                    "volume": volume, "strength": strength}, now=at)


def trigger(strat, code, t0, price=10_000):
    """무장(1.5초) + 버스트 -> 되돌림 대기 계획 생성까지."""
    tick(strat, code, 130.0, t0, price=price)
    feed(strat.phase1b.trade_flow, code,
         SM.PHASE1A_BURST_TRADE_COUNT,
         SM.PHASE1A_BURST_TRADE_VALUE * SM.burst_price_scale(price) * 1.05,
         now=t0 + 3.5, price=price)
    tick(strat, code, 130.0, t0 + 3.5, price=price)


T = time.time()

# ═════════════════════════════════════════════════════════
print("\n[1] [A] A-1 — _today_open 조회 창 절단 가드")
# ═════════════════════════════════════════════════════════
s = build(datetime(2026, 8, 6, 9, 18, 21))

# 08-06 09:18 실제 상황: 분봉 15개 -> 창이 09:03~09:18 (09:00봉 없음)
trunc = [{"time_str": f"2026080609{m:02d}00",
          "open": 7_650 if m == 3 else 8_000, "close": 8_210}
         for m in range(18, 2, -1)]
check("[재현] 창이 절단되면 0.0 (구버전은 09:03봉의 7,650을 반환했다)",
      s._today_open(trunc) == 0.0, str(s._today_open(trunc)))

# 대조군 ①: 09:00봉이 창에 있으면 예전처럼 정상
full = [{"time_str": f"2026080609{m:02d}00",
         "open": 7_200 if m == 0 else 8_000, "close": 8_210}
        for m in range(18, -1, -1)]
check("[대조군] 09:00봉이 있으면 정상 반환(7,200)", s._today_open(full) == 7_200,
      str(s._today_open(full)))

# 대조군 ②: 전일 봉이 섞여 있으면 창이 하루 전체를 덮은 것 -> 첫 봉을 신뢰한다.
# (거래가 뜸해 09:00봉 자체가 없는 종목이 여기 해당한다 — 막으면 안 된다)
mixed = [{"time_str": "20260806090300", "open": 7_650, "close": 8_210},
         {"time_str": "20260805150000", "open": 7_000, "close": 7_100}]
check("[대조군] 전일봉이 섞이면 오늘 첫 봉을 신뢰(창이 안 잘림)",
      s._today_open(mixed) == 7_650, str(s._today_open(mixed)))

# 기존 계약 유지
check("당일 봉이 없으면 0.0",
      s._today_open([{"time_str": "20260805150000", "open": 7_000}]) == 0.0)
check("빈 리스트도 안전하게 0.0", s._today_open([]) == 0.0)
check("time_str 포맷이 깨져도 예외 없이 0.0",
      s._today_open([{"time_str": "2026080609", "open": 7_000}]) == 0.0)

# 09:00봉이 창의 유일한 봉이어도 통과해야 한다(개장 직후)
just_open = [{"time_str": "20260806090000", "open": 7_200, "close": 7_250}]
check("[경계] 09:00봉 하나뿐이어도 정상 반환", s._today_open(just_open) == 7_200)

# ═════════════════════════════════════════════════════════
print("\n[2] [A] A-2 — 호출부가 '캐시 우선'인가")
# ═════════════════════════════════════════════════════════
# 구/신 식의 차이를 값으로 못박는다. 캐시(ka10001 open_pric)가 거래소 확정
# 시가라 분봉 추정보다 항상 정확하다.
cache_open, wrong_open = 7_200.0, 7_650.0
check("[구버전 재현] `틀린값 or 캐시` -> 틀린 값이 이긴다",
      (wrong_open or cache_open) == wrong_open)
check("[신버전] `캐시 or 틀린값` -> 정확한 값이 이긴다",
      (cache_open or wrong_open) == cache_open)

# ⚠️ 소스 문자열이나 추측한 메서드명으로 단언하지 않는다(08-06 감사 교훈).
#    실제 호출·반환값으로만 검증한다.
s2 = build(datetime(2026, 8, 6, 9, 18, 21))
setup(s2, "073240", cond="돌파자동매매용", open_px=None)
s2._opening_prices["073240"] = 7_200.0      # 09:00:55에 정확히 캐시된 값
s2._prev_closes["073240"] = 7_361.0
s2.api.candles = trunc                       # 절단된 분봉만 돌려주는 상황
ok = s2._evaluate_1a_pullback_entry(
    "073240", "금호타이어", 1, trunc, 8_210, wrong_open,
    datetime(2026, 8, 6, 9, 18, 21).time(),
)
check("[차단] 캐시 시가 7,200 기준 +14.03%라 매수되지 않음",
      ok is False and "073240" not in s2.holdings)
rej = " ".join(s2._reject_reasons.get("073240", [])) \
    if hasattr(s2, "_reject_reasons") else ""
check("[차단] 시가 캐시가 살아있다(틀린 값으로 덮이지 않음)",
      s2._opening_prices["073240"] == 7_200.0,
      str(s2._opening_prices["073240"]))

# 시가대비 필터 자체의 경계 — evaluate_tick_entry의 실제 반환 사유로 본다.
s2c = build(datetime(2026, 8, 6, 9, 18, 21))
setup(s2c, "SG", cond="돌파자동매매용", open_px=None)
s2c.phase1b.start_watching("SG")
s2c.update_strength_timer("SG", 130.0, now=T - 10)     # 무장시켜 시가 게이트까지 도달
_, info_hi = s2c.evaluate_tick_entry("SG", "1A", 8_210, open_price=7_200,
                                     cond_name="돌파자동매매용",
                                     now_dt=datetime(2026, 8, 6, 9, 18, 21))
check("[차단] 진짜 시가 7,200 -> +14.0%라 '매수 보류' 사유가 나온다",
      "시가대비" in info_hi.get("reason", ""), info_hi.get("reason", ""))
_, info_lo = s2c.evaluate_tick_entry("SG", "1A", 8_210, open_price=7_650,
                                     cond_name="돌파자동매매용",
                                     now_dt=datetime(2026, 8, 6, 9, 18, 21))
check("[구버전 재현] 절단값 7,650 -> +7.3%라 시가 게이트를 통과했다",
      "시가대비" not in info_lo.get("reason", ""), info_lo.get("reason", ""))

# ═════════════════════════════════════════════════════════
print("\n[3] [B] 버스트 side 구분 — 투매를 매수 신호로 읽지 않는가")
# ═════════════════════════════════════════════════════════
check("BURST_REQUIRE_BUY_SIDE 활성", SM.BURST_REQUIRE_BUY_SIDE is True)

tf = TradeFlowTracker()
nw = time.time()
BIG = 50_000_000
# 매도만 3건
for i in range(3):
    tf.add_tick("SELLONLY", 10_000, "sell", BIG // 10_000, now=nw - i * 0.3)
check("[핵심] 매도 대량체결은 side='buy'로 세면 0건",
      tf.count_large_trades("SELLONLY", 5.0, BIG * 0.9, now=nw, side="buy") == 0)
check("[구버전 재현] side 미지정이면 매도도 3건으로 셌다",
      tf.count_large_trades("SELLONLY", 5.0, BIG * 0.9, now=nw) == 3)
check("[핵심] 매도 대량체결은 max_single도 0",
      tf.max_single_trade_value("SELLONLY", 5.0, now=nw, side="buy") == 0.0)
check("[구버전 재현] side 미지정이면 max_single이 잡혔다",
      tf.max_single_trade_value("SELLONLY", 5.0, now=nw) >= BIG * 0.9)

# 대조군 — 매수면 기존과 완전히 동일
for i in range(3):
    tf.add_tick("BUYONLY", 10_000, "buy", BIG // 10_000, now=nw - i * 0.3)
check("[대조군] 매수 대량체결은 기존과 동일하게 3건",
      tf.count_large_trades("BUYONLY", 5.0, BIG * 0.9, now=nw, side="buy") == 3)
check("[대조군] side 미지정과 결과가 같다",
      tf.count_large_trades("BUYONLY", 5.0, BIG * 0.9, now=nw, side="buy")
      == tf.count_large_trades("BUYONLY", 5.0, BIG * 0.9, now=nw))

# neutral — 파서가 volume=abs(signed)로 넣으므로 neutral이면 volume 0이다.
# 즉 원리상 어떤 문턱도 못 넘는다. '중립이라 놓친다'는 걱정은 근거가 없다.
tf.add_tick("NEUTRAL", 10_000, "neutral", 0, now=nw)
check("[한계 확인] neutral은 volume 0이라 side 무관하게 버스트에 못 낀다",
      tf.count_large_trades("NEUTRAL", 5.0, 1.0, now=nw) == 0
      and tf.count_large_trades("NEUTRAL", 5.0, 1.0, now=nw, side="buy") == 0)

# 혼재 — 매수 1건 + 매도 2건이면 절대경로(2건)를 못 넘는다
for i in range(2):
    tf.add_tick("MIX", 10_000, "sell", BIG // 10_000, now=nw - i * 0.2)
tf.add_tick("MIX", 10_000, "buy", BIG // 10_000, now=nw)
check("[혼재] 매수 1 + 매도 2 -> 매수 기준 1건뿐이라 2건 조건 미달",
      tf.count_large_trades("MIX", 5.0, BIG * 0.9, now=nw, side="buy") == 1)

# 기본값이 None이라 다른 호출부(candidate_tier 등)는 안 바뀐다
import inspect as _i
sig_c = _i.signature(TradeFlowTracker.count_large_trades)
sig_m = _i.signature(TradeFlowTracker.max_single_trade_value)
check("count_large_trades의 side 기본값은 None (기존 호출부 불변)",
      sig_c.parameters["side"].default is None)
check("max_single_trade_value의 side 기본값은 None",
      sig_m.parameters["side"].default is None)

# check_burst 실경로 — 매도만으로는 발화하지 않는다
s3 = build()
setup(s3, "SB")
feed(s3.phase1b.trade_flow, "SB", 4,
     SM.PHASE1A_BURST_TRADE_VALUE * 1.5, now=T, side="sell")
ok_b, det = s3.check_burst("SB", now=T)
check("[경로] check_burst — 매도 대량체결만으로는 발화 안 함", ok_b is False,
      str(det.get("burst_count")))
s3b = build()
setup(s3b, "BB")
feed(s3b.phase1b.trade_flow, "BB", SM.PHASE1A_BURST_TRADE_COUNT,
     SM.PHASE1A_BURST_TRADE_VALUE * 1.5, now=T, side="buy")
ok_b2, det2 = s3b.check_burst("BB", now=T)
check("[대조군] 같은 규모가 매수면 정상 발화", ok_b2 is True, str(det2))

# ═════════════════════════════════════════════════════════
print("\n[4] [C] 등락률 하한 — 매수는 + 일 때만")
# ═════════════════════════════════════════════════════════
check("MIN_ENTRY_CHANGE_PCT == 0.0 (사용자 지정)",
      abs(SM.MIN_ENTRY_CHANGE_PCT - 0.0) < 1e-9)
check("하한 < 상한 (뒤집히면 아무것도 못 산다)",
      SM.MIN_ENTRY_CHANGE_PCT < SM.MAX_ENTRY_CHANGE_PCT_PULLBACK
      < SM.MAX_ENTRY_CHANGE_PCT or
      SM.MIN_ENTRY_CHANGE_PCT < SM.MAX_ENTRY_CHANGE_PCT_PULLBACK)

s4 = build(change_rate=-1.26)   # 08-06 주성엔지니어링 실측
setup(s4, "036930")
r = s4._entry_change_reject("036930", "1A", 10_000)
check("[핵심] 전일종가 대비 -1.26%면 매수 거절", r is not None and "하한" in r, str(r))

s4b = build(change_rate=0.25)   # 08-06 금호타이어 실측(승자 +2.85%)
setup(s4b, "073240")
check("[승자 보존] +0.25%(08-06 금호타이어)는 통과",
      s4b._entry_change_reject("073240", "1A", 10_000) is None)

for cr, name in ((7.90, "코스모로보틱스 +3.17%"), (4.61, "현대약품 +4.25%"),
                 (3.69, "GS건설 +0.46%")):
    sw = build(change_rate=cr)
    setup(sw, "W")
    check(f"[승자 보존] {name} (전일대비 {cr:+.2f}%) 통과",
          sw._entry_change_reject("W", "1A", 10_000) is None)

s4c = build(change_rate=0.0)
setup(s4c, "ZERO")
check("[경계] 정확히 0.00%는 거절 (+ '초과'여야 매수)",
      s4c._entry_change_reject("ZERO", "1A", 10_000) is not None)

s4d = build(change_rate=15.0)
setup(s4d, "HIGH")
r2 = s4d._entry_change_reject("HIGH", "1A", 10_000)
check("[상한 유지] +15%는 여전히 상한으로 거절", r2 is not None and "상한" in r2, str(r2))

s4e = build(change_rate=11.0)
setup(s4e, "PB")
check("[전략별 상한 유지] 눌림목(10%)에서 +11%는 거절",
      s4e._entry_change_reject("PB", "1A_눌림", 10_000) is not None)
check("[전략별 상한 유지] 같은 +11%가 1A(13%)에서는 통과",
      s4e._entry_change_reject("PB", "1A", 10_000) is None)


class _NoPrev(_Rest):
    def get_stock_change_rate(self, code): return None
    def get_basic_quote(self, code): return {}


s4f = build()
s4f.api = _NoPrev()
setup(s4f, "UNK")
s4f._prev_closes.pop("UNK", None)
check("[모름은 막지 않는다] 전일종가를 못 구하면 통과(기존 규약)",
      s4f._entry_change_reject("UNK", "1A", 10_000) is None)

# 규칙이 한 곳에만 있는가 — 세 경로가 전부 같은 함수를 쓰는지 동작으로 확인
s4g = build(change_rate=-2.0)
setup(s4g, "NEG")
s4g._open_entry_plan("NEG", "NEG", 1, {"current_price": 10_000}, "1A",
                     "주도주상위", trigger_price=10_000, now=T)
check("[경로1] 하락 중이면 되돌림 대기 계획조차 안 건다(슬롯 낭비 방지)",
      "NEG" not in s4g._entry_plans)
s4g._execute_buy("NEG", "NEG", 1, {"current_price": 10_000}, sub_strategy="1A")
check("[경로2] 주문 직전 하드가드에서도 차단", "NEG" not in s4g.holdings)

cat = SM.StrategyManager._reject_category(
    "등락률 하한 미달 (전일종가대비 -1.3% <= +0%)")
check("[진단] 하한 미달이 '기타'로 뭉개지지 않는다", cat == "등락률 하한 미달(하락중)", cat)
check("[진단] 상한 초과와 다른 분류로 갈린다",
      cat != SM.StrategyManager._reject_category(
          "등락률 상한 초과 (전일종가대비 +15.0% > +13%)"))

# ═════════════════════════════════════════════════════════
print("\n[5] [D] 상승 이탈 — OFF")
# ═════════════════════════════════════════════════════════
check("ENTRY_BREAKOUT_ENABLED == False (08-06 실측: 평균 -1.00%, 승률 22%)",
      SM.ENTRY_BREAKOUT_ENABLED is False)
check("로직/상수는 보존 — True 한 줄로 복귀 가능",
      isinstance(SM.ENTRY_BREAKOUT_PCT, float) and SM.ENTRY_BREAKOUT_PCT > 0)

s5 = build()
setup(s5, "BO")
trigger(s5, "BO", T)
check("[사전] 되돌림 대기 계획 생성됨", "BO" in s5._entry_plans)
plan = s5._entry_plans["BO"]
trig_px = plan["trigger_price"]
# 트리거 대비 +0.5% (구버전이면 여기서 잔량 전량 즉시 체결됐다)
up_px = int(trig_px * 1.005)
s5._try_fill_entry_plan("BO", up_px, now=T + 5)
check("[핵심] +0.5% 상승해도 즉시 매수하지 않는다(추격매수 차단)",
      "BO" not in s5.holdings, str(list(s5.holdings)))
check("[핵심] 계획은 유지 — 되돌림은 계속 기다린다", "BO" in s5._entry_plans)

# 대조군 — 되돌림은 그대로 작동해야 한다
down_px = int(trig_px * (1.0 - SM.ENTRY_PULLBACK_TRANCHES[0][0]) - 1)
s5._try_fill_entry_plan("BO", down_px, now=T + 6)
check("[대조군] 되돌림 1차는 정상 체결된다", "BO" in s5.holdings,
      str(list(s5.holdings)))

# 08-06 발동 폭 분포 — 문턱 상향이 대안이 못 된다는 근거를 못박는다
fired = [0.30, 0.31, 0.32, 0.32, 0.33, 0.35, 0.36, 0.36, 0.37, 0.37,
         0.41, 0.44, 0.48, 0.49, 0.52]
check("[근거] 08-06 발동 15건이 전부 0.30~0.52%에 몰려 있다",
      min(fired) >= 0.30 and max(fired) <= 0.52 and len(fired) == 15)
check("[근거] 문턱 0.6%면 15건 전부 탈락 = 끄는 것과 동일",
      sum(1 for p in fired if p >= 0.60) == 0)

# ═════════════════════════════════════════════════════════
print("\n[6] 상호작용 — [C]와 [D]가 손절/청산을 건드리지 않는가")
# ═════════════════════════════════════════════════════════
s6 = build()
setup(s6, "SL")
s6.holdings["SL"] = {
    "trade_id": 1, "buy_price": 10_000, "origin_price": 10_000,
    "buy_quantity": 100, "qty": 100, "buy_time": s6._now(),
    "stock_name": "SL", "strategy_phase": "1A", "sub_strategy": "1A",
    "highest_price": 10_000, "lowest_price": 10_000, "ma20": None,
    "ma20_updated": None,
    "warmup_until": s6._now() + timedelta(seconds=999),   # 워밍업 중
}
s6.on_price_update("SL", 9_600)   # -4%
check("[불변] 손절은 워밍업 중에도, 하한 규칙과 무관하게 작동",
      "SL" not in s6.holdings, str(list(s6.holdings)))

# 하한은 '매수'에만 적용된다 — 이미 보유한 종목이 마이너스가 됐다고
# 매수 규칙이 청산을 트리거하면 안 된다.
s6b = build(change_rate=-5.0)
setup(s6b, "HOLD")
s6b.holdings["HOLD"] = dict(s6.holdings.get("SL", {}) or {}, **{
    "trade_id": 2, "buy_price": 10_000, "origin_price": 10_000,
    "buy_quantity": 100, "qty": 100, "buy_time": s6b._now(),
    "stock_name": "HOLD", "strategy_phase": "1A", "sub_strategy": "1A",
    "highest_price": 10_000, "lowest_price": 10_000, "ma20": None,
    "ma20_updated": None,
    "warmup_until": s6b._now() - timedelta(seconds=1),
})
s6b.on_price_update("HOLD", 9_950)   # -0.5%, 손절선 위
check("[불변] 전일 대비 마이너스여도 보유분을 강제 청산하지 않는다",
      "HOLD" in s6b.holdings)

# ═════════════════════════════════════════════════════════
print("\n[7] 상수 정합성")
# ═════════════════════════════════════════════════════════
check("무장 시간 < 무장 TTL", SM.TICK_STRENGTH_SUSTAIN_SEC < SM.TICK_ARMED_TTL_SEC
      if hasattr(SM, "TICK_ARMED_TTL_SEC") else True)
check("주가계수 상한 3.0", abs(SM.BURST_PRICE_MAX - 3.0) < 1e-9)
check("주가계수 하한 0.3", abs(SM.BURST_PRICE_MIN - 0.3) < 1e-9)
check("시가대비 상한 8.0", abs(SM.PHASE1A_LEADING_OPEN_SURGE_CAP - 8.0) < 1e-9)
check("급등 강화 시작 6.0 < 시가대비 상한 8.0",
      SM.PHASE1A_OPEN_SURGE_STRICT_FROM < SM.PHASE1A_LEADING_OPEN_SURGE_CAP)
check("1A 등락률 상한 13.0", abs(SM.MAX_ENTRY_CHANGE_PCT - 13.0) < 1e-9)
check("눌림 등락률 상한 10.0", abs(SM.MAX_ENTRY_CHANGE_PCT_PULLBACK - 10.0) < 1e-9)
check("VI 매수차단은 OFF 유지", SM.VI_UPPER_ENTRY_BLOCK_ENABLED is False)
check("VI 확정매도는 ON 유지", SM.VI_UPPER_EXIT_ENABLED is True)
check("본전스톱 ON 유지", SM.BREAKEVEN_STOP_ENABLED is True)
check("되돌림 대기 ON 유지 (상승이탈만 껐다)", SM.ENTRY_PULLBACK_ENABLED is True)

# ═════════════════════════════════════════════════════════
print("\n" + "=" * 62)
print(f"통과 {len(PASS)} / 실패 {len(FAIL)}")
if FAIL:
    print("\n실패 목록:")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
