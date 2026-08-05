"""2026-08-05 장중 수술 7건 검증 — 특히 신규 기능 3건.

  #3 손절 대신 추가매수 (Rescue Add)   <- 손절이라는 최후 방어선을 건드린다
  #6 '놓친 기회' 알림
  #7 되돌림 대기 중 상승 이탈 즉시진입

#3은 이 코드베이스에서 가장 위험한 변경이다. "조건 3개가 전부 성립할 때만
발동하고, 나머지 모든 경우(조건 불충족/한도 초과/예외/차단상태)는 예외 없이
손절로 수렴한다"를 경계값까지 못박는다.

실행: python test_patch_20260805.py   (종료코드 0 = 전원 통과)
"""
import os as _os_testlog
_os_testlog.environ["AUTOTRADER_TEST_LOG"] = "1"

import inspect as _insp
import sys
import time
from datetime import datetime, timedelta

import core.strategy_manager as SM
from core.phase1b_controller import Phase1BController

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


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
    def update(cls, i, d): cls.updates.append({"id": i, **d}); return True
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
    def is_leading_theme_stock(self, c): return False


class _Rest:
    host = "https://api.kiwoom.com"
    def __init__(self): self.calls = []
    def get_minute_candles(self, code, interval=1, count=1, base_date=None):
        return [{"time_str": "20260805090000", "open": 9_990, "high": 10_010,
                 "low": 9_980, "close": 10_000, "volume": 1000}] * max(count, 20)
    def get_orderable_amount(self): return 10_835_694
    def get_stock_change_rate(self, code): return 3.0
    def get_index_change_rate(self, s="001"): return 0.0
    def get_current_price(self, code): return 10_000


class _OrderMgr:
    def __init__(self): self.orders = []
    def buy(self, code, qty, price=0, sizing="REGULAR", exit_strategy="REGULAR",
            order_style="limit", ref_price=0):
        self.orders.append({"code": code, "qty": qty, "side": "buy"})
        return {"success": True, "ord_no": "1", "price": ref_price or 10_000,
                "style": order_style}
    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"code": code, "qty": qty, "side": "sell"})
        return {"success": True, "ord_no": "2", "price": price or 10_000,
                "style": order_style}
    def get_stock_name(self, code): return code


class Clock:
    def __init__(self, dt): self.dt = dt
    def __call__(self): return self.dt
    def set(self, h, m, s=0): self.dt = self.dt.replace(hour=h, minute=m, second=s)


def build(now_dt=datetime(2026, 8, 5, 10, 0, 0)):
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows, _Repo.sells, _Repo.updates = [], [], []
    clock = Clock(now_dt)
    s = SM.StrategyManager(kiwoom_rest=_Rest(), order_manager=_OrderMgr(),
                           phase1b_controller=Phase1BController(),
                           portfolio_optimizer=None, now_func=clock)
    return s, clock


def put_pos(s, code="R1", buy=10_000, qty=100, warm=False):
    """보유 포지션을 만든다. warm=False면 워밍업 종료 상태(기준선 앵커 가능)."""
    s.holdings[code] = {
        "trade_id": 1, "buy_price": buy, "origin_price": buy,
        "buy_quantity": qty, "qty": qty, "buy_time": s._now(),
        "stock_name": code, "strategy_phase": "1A", "sub_strategy": "1A",
        "highest_price": buy, "lowest_price": buy, "ma20": None,
        "ma20_updated": None,
        "warmup_until": s._now() + timedelta(seconds=(999 if warm else -1)),
    }
    return s.holdings[code]


def feed(s, code, *, accel=True, strength=True, rebound=True, base=100.0):
    """추가매수 조건 3개를 원하는 대로 만들어 주는 틱 주입."""
    s.phase1b.start_watching(code)
    tf = s.phase1b.trade_flow
    now = time.time()
    # 오래된 구간: 작은 체결 (분모)
    for i in range(40):
        tf.add_tick(code, 10_000, "buy", 1, now=now - 110 + i)
    # 최근 30초: accel이면 대량 체결
    big = 400 if accel else 1
    for i in range(10):
        tf.add_tick(code, 9_700, "sell", 1, now=now - 28 + i)   # 저점 형성
    for i in range(10):
        px = 9_730 if rebound else 9_700
        side = "buy" if strength else "sell"
        tf.add_tick(code, px, side, big, now=now - 10 + i)
    pos = s.holdings.get(code)
    if pos is not None:
        pos["strength_baseline"] = base
    return tf


T0 = time.time()

# ═════════════════════════════════════════════════════════
print("\n[1] 상수 — 7건이 전부 반영됐는가")
# ═════════════════════════════════════════════════════════
check("① 시장가 문턱 1천만", SM.PHASE1A_ASK_DEPTH_MIN == 10_000_000,
      f"{SM.PHASE1A_ASK_DEPTH_MIN:,}")
check("② 본전스톱 바닥 +0.2%", abs(SM.BREAKEVEN_FLOOR - 0.002) < 1e-9)
check("④ 1A 등락률 13% / 눌림 10%",
      SM.MAX_ENTRY_CHANGE_PCT == 13.0 and SM.MAX_ENTRY_CHANGE_PCT_PULLBACK == 10.0)
check("⑤ 되돌림 -0.3%/-0.7%",
      SM.ENTRY_PULLBACK_TRANCHES == ((0.003, 0.5), (0.007, 0.5)))
check("⑦ 상승 이탈 +0.3% 활성",
      SM.ENTRY_BREAKOUT_ENABLED is True and abs(SM.ENTRY_BREAKOUT_PCT - 0.003) < 1e-9)
check("③ 추가매수 상수", SM.RESCUE_ADD_ENABLED is True
      and SM.RESCUE_ADD_ACCEL_MIN == 3.0
      and SM.RESCUE_ADD_MIN_STRENGTH == 100.0
      and abs(SM.RESCUE_ADD_REBOUND_PCT - 0.003) < 1e-9
      and abs(SM.RESCUE_ADD_FINAL_STOP - 0.06) < 1e-9
      and SM.RESCUE_ADD_MAX_PER_DAY == 2)
check("되돌림 1차 폭 == 상승 이탈 폭 (±0.3% 대칭 밴드)",
      abs(SM.ENTRY_PULLBACK_TRANCHES[0][0] - SM.ENTRY_BREAKOUT_PCT) < 1e-9)
check("최종손절(-6%)이 일반손절(-3%)보다 깊다",
      SM.RESCUE_ADD_FINAL_STOP > abs(SM.STOP_LOSS_RATE))

# ═════════════════════════════════════════════════════════
print("\n[2] #3 추가매수 — 관찰 창 -> 반등 확증 시에만 발동")
# ═════════════════════════════════════════════════════════
# ⚠️ 손절은 -3%에 **최초로 닿는 순간** 발동하므로 그 시점엔 현재가=최근저점이라
#    "저점 대비 +0.3% 반등"이 정의상 0%다. 그래서 ①②가 성립하면 매도를 잠시
#    보류하고(관찰 창) 그 안에서 반등을 확인한다. 이 순서를 그대로 재현한다.
s, _ = build()
pos = put_pos(s)
feed(s, "R1")
s.on_price_update("R1", 9_690)          # 첫 -3.1% 도달
sells = [o for o in s.order_manager.orders if o["side"] == "sell"]
check("①② 충족 -> 첫 도달에 매도하지 않고 관찰 시작",
      not sells and pos.get("rescue_watch_until") is not None,
      f"매도 {len(sells)}건")
check("관찰 저점이 기록됨", pos.get("rescue_low") == 9_690)

s.on_price_update("R1", 9_650)          # 더 밀림 — 저점 갱신, 아직 반등 아님
check("관찰 중 저점 갱신", pos.get("rescue_low") == 9_650)
check("아직 매도도 매수도 없음",
      not [o for o in s.order_manager.orders if o["side"] in ("sell", "buy")])

s.on_price_update("R1", 9_690)          # 저점 대비 +0.41%, 여전히 -3.1%
buys = [o for o in s.order_manager.orders if o["side"] == "buy"]
check("저점 대비 +0.3% 반등 -> 추가매수 집행", len(buys) == 1,
      f"매수 {len(buys)}건")
check("rescue_added 표시됨", s.holdings.get("R1", {}).get("rescue_added") is True)
check("일일 카운터 증가", s._rescue_count_today == 1)
check("원가(origin_price)는 평단으로 덮이지 않음",
      s.holdings["R1"]["origin_price"] == 10_000,
      f'평단 {s.holdings["R1"]["buy_price"]:,.0f} / 원가 '
      f'{s.holdings["R1"]["origin_price"]:,.0f}')

for nm, kw in (("거래대금 미달", dict(accel=False)),
               ("강도 미달", dict(strength=False))):
    s2, _ = build()
    put_pos(s2, "R2")
    feed(s2, "R2", **kw)
    s2.on_price_update("R2", 9_690)
    sold = [o for o in s2.order_manager.orders if o["side"] == "sell"]
    check(f"①② 중 {nm} -> 관찰도 안 하고 즉시 손절",
          len(sold) == 1 and "R2" not in s2.holdings, f"매도 {len(sold)}건")

# ═════════════════════════════════════════════════════════
print("\n[3] #3 관찰 창의 출구 — 하한 이탈 / 만료 / 반등 없음")
# ═════════════════════════════════════════════════════════
sA, _ = build()
pA = put_pos(sA, "RA")
feed(sA, "RA")
sA.on_price_update("RA", 9_690)
check("관찰 시작됨", pA.get("rescue_watch_until") is not None)
sA.on_price_update("RA", 9_540)          # 원가 -4.6% (하한 -4.5% 이탈)
soldA = [o for o in sA.order_manager.orders if o["side"] == "sell"]
check("관찰 중 하한(-4.5%) 이탈 -> 즉시 손절",
      len(soldA) == 1 and "RA" not in sA.holdings)

sB, _ = build()
pB = put_pos(sB, "RB")
feed(sB, "RB")
sB.on_price_update("RB", 9_690)
pB["rescue_watch_until"] = time.time() - 1     # 창 만료 상태로
sB.on_price_update("RB", 9_695)
soldB = [o for o in sB.order_manager.orders if o["side"] == "sell"]
check("관찰 창 만료(반등 없음) -> 손절",
      len(soldB) == 1 and "RB" not in sB.holdings)

# ═════════════════════════════════════════════════════════
print("\n[4] #3 한도 / 차단 / 예외는 전부 손절로 수렴")
# ═════════════════════════════════════════════════════════
s4, _ = build()
s4._rescue_count_today = SM.RESCUE_ADD_MAX_PER_DAY
put_pos(s4, "R4")
feed(s4, "R4")
s4.on_price_update("R4", 9_690)
check("하루 한도 소진 -> 관찰 없이 손절",
      len([o for o in s4.order_manager.orders if o["side"] == "sell"]) == 1
      and "R4" not in s4.holdings)

s5, _ = build()
put_pos(s5, "R5")
feed(s5, "R5")
s5._entry_block_reason = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
s5.on_price_update("R5", 9_690)
check("판정 중 예외 -> 손절로 수렴 (최후 방어선 유지)",
      len([o for o in s5.order_manager.orders if o["side"] == "sell"]) == 1
      and "R5" not in s5.holdings)

s6, _ = build()
put_pos(s6, "R6")
feed(s6, "R6")
s6._entry_block_reason = lambda: "MDD 일손실 차단"
s6.on_price_update("R6", 9_690)
check("MDD/가드 차단 중 -> 관찰 없이 손절",
      len([o for o in s6.order_manager.orders if o["side"] == "sell"]) == 1)

s3, _ = build()
p3 = put_pos(s3, "R3")
feed(s3, "R3")
s3.on_price_update("R3", 9_690)
s3.on_price_update("R3", 9_650)
s3.on_price_update("R3", 9_690)
check("1회차 추가매수 성공", p3.get("rescue_added") is True)
s3.order_manager.orders.clear()
s3.on_price_update("R3", 9_500)          # 평단 -3% 재도달, 원가 -6% 전
check("같은 종목 2회차 추가매수는 없음",
      not [o for o in s3.order_manager.orders if o["side"] == "buy"])

# ═════════════════════════════════════════════════════════
print("\n[5] #3 최종 방어선 — 원가 -6%면 무조건 청산")
# ═════════════════════════════════════════════════════════
s7, _ = build()
p7 = put_pos(s7, "R7")
feed(s7, "R7")
s7.on_price_update("R7", 9_690)
s7.on_price_update("R7", 9_650)
s7.on_price_update("R7", 9_690)
check("추가매수 완료", p7.get("rescue_added") is True)
s7.order_manager.orders.clear()
s7.on_price_update("R7", 9_450)          # 원가 -5.5% (아직 -6% 전)
check("원가 -6% 전에는 청산하지 않음(버틴다)",
      not [o for o in s7.order_manager.orders if o["side"] == "sell"],
      f'평단 {s7.holdings["R7"]["buy_price"]:,.0f}')
s7.on_price_update("R7", 9_390)          # 원가 -6.1%
sold7 = [o for o in s7.order_manager.orders if o["side"] == "sell"]
check("원가 -6% 도달 -> 무조건 전량 청산", len(sold7) == 1 and "R7" not in s7.holdings)

# ═════════════════════════════════════════════════════════
print("\n[5] #7 상승 이탈 — 되돌림 대기 중 +0.3% 돌파")
# ═════════════════════════════════════════════════════════
def make_plan(sm, code="B1", trig=10_000):
    sm.phase1b.start_watching(code)
    sm.phase1b.orderbook.update(code, {"ask_prices": [trig, trig + 10, trig + 20],
                                       "ask_volumes": [3_000, 3_000, 3_000]},
                                now=time.time())
    sm._stock_names[code] = code
    sm._cond_names[code] = "주도주상위"
    sm._open_entry_plan(code, code, 1, {"current_price": trig}, "1A",
                        "주도주상위", trig, now=time.time())

s8, _ = build()
make_plan(s8)
check("계획 생성됨", "B1" in s8._entry_plans)
s8._try_fill_entry_plan("B1", 10_029, now=time.time())    # +0.29%
check("+0.29%로는 발동하지 않음(경계 아래)",
      "B1" not in s8.holdings and "B1" in s8._entry_plans)
s8._try_fill_entry_plan("B1", 10_030, now=time.time())    # +0.30%
check("+0.30% 돌파 -> 즉시 전량 체결", "B1" in s8.holdings)
check("계획이 닫힘(슬롯 반환)", "B1" not in s8._entry_plans)
check("한 번에 전량(트랜치 2개 모두)",
      s8.holdings["B1"].get("tranches_filled", 1) == 1
      and s8.holdings["B1"]["qty"] > 0)

s9, _ = build()
make_plan(s9, "B2")
s9.api.get_stock_change_rate = lambda c: 20.0     # 등락률 20% (1A 상한 13% 초과)
s9._prev_closes = {}
s9._try_fill_entry_plan("B2", 10_030, now=time.time())
check("상승 이탈해도 등락률 상한 초과면 미집행",
      "B2" not in s9.holdings, str(list(s9.holdings)))

s10, _ = build()
make_plan(s10, "B3")
s10._try_fill_entry_plan("B3", 9_970, now=time.time())    # 1차 목표(-0.3%)
check("하락 도달이 우선 — 1차 트랜치 체결", "B3" in s10.holdings)
check("계획 유지(2차 대기)", "B3" in s10._entry_plans)

# ═════════════════════════════════════════════════════════
print("\n[6] #6 놓친 기회 알림 — 계층 분리")
# ═════════════════════════════════════════════════════════
s11, clock11 = build()
s11._stock_names.update({"M1": "완벽신호", "M2": "대금부족", "M3": "고등락률"})
s11._note_reject("M1", "되돌림 미도달 (120초 내 -0.3% 미달)")
s11._note_reject("M2", "대량체결 부족")
s11._note_reject("M3", "등락률 상한 초과 (전일종가대비 +18.5% > +10%)")
body = s11.build_missed_opportunities()
check("되돌림 미도달이 최상단(🥇)", body.index("🥇") < body.index("⛔"))
check("등락률 초과는 최하단(⛔) + 비권장 표기",
      "⛔" in body and "참고만" in body)
check("종목명이 코드가 아니라 이름으로 표시", "완벽신호" in body)
check("등락률 초과가 '매수 컷오프'로 뭉개지지 않음",
      SM.StrategyManager._reject_category(
          "등락률 상한 초과 (전일종가대비 +18.5% > +10%)") == "등락률 상한 초과")
s12, _ = build()
check("놓친 게 없으면 '없음' 문구(알림 미발송 조건)",
      "아깝게 놓친 후보 없음" in s12.build_missed_opportunities())

# ═════════════════════════════════════════════════════════
print("\n[7] 회귀 — 손절이 여전히 최후 방어선인가")
# ═════════════════════════════════════════════════════════
s13, _ = build()
put_pos(s13, "R8", warm=True)            # 워밍업 중
feed(s13, "R8", accel=False)             # 추가매수 조건 불충족
s13.on_price_update("R8", 9_690)
check("워밍업 중에도 손절은 작동",
      len([o for o in s13.order_manager.orders if o["side"] == "sell"]) == 1)

s14, _ = build()
put_pos(s14, "R9")
feed(s14, "R9")
SM.RESCUE_ADD_ENABLED = False
s14.on_price_update("R9", 9_690)
check("RESCUE_ADD_ENABLED=False면 구버전대로 손절",
      len([o for o in s14.order_manager.orders if o["side"] == "sell"]) == 1)
SM.RESCUE_ADD_ENABLED = True

s15, _ = build()
make_plan(s15, "B4")
SM.ENTRY_BREAKOUT_ENABLED = False
s15._try_fill_entry_plan("B4", 10_050, now=time.time())
check("ENTRY_BREAKOUT_ENABLED=False면 상승 이탈 미발동",
      "B4" not in s15.holdings)
SM.ENTRY_BREAKOUT_ENABLED = True

# ═════════════════════════════════════════════════════════
print("\n[9] 시가대비 필터 — ka10001 1콜로 채워지는가 (REST 추가 0콜)")
# ═════════════════════════════════════════════════════════
# 08-05 실측: 이 필터의 발동 로그가 하루 종일 0건이었고 PS일렉트로닉스가
# 시가대비 +9.94%인데 그대로 체결됐다. 원인은 시가 캐시가 비어 있으면
# open_price=0이 되어 필터가 통째로 스킵되는 것. 이제 전일종가를 가져오는
# 바로 그 ka10001 응답에서 시가까지 함께 캐시한다.
class _RestQuote(_Rest):
    def __init__(self):
        super().__init__(); self.basic_calls = 0
    def get_basic_quote(self, code):
        self.basic_calls += 1
        return {"change_rate": 3.0, "open": 9_000.0}

s16, _ = build()
s16.api = _RestQuote()
pc = s16._get_prev_close("Q1", 10_000)
check("전일종가 조회 1콜로 시가까지 캐시됨",
      s16._opening_prices.get("Q1") == 9_000.0 and s16.api.basic_calls == 1,
      f'시가={s16._opening_prices.get("Q1")} / ka10001 {s16.api.basic_calls}콜')
check("전일종가도 정상 산출", pc and abs(pc - 10_000/1.03) < 1)
before = s16.api.basic_calls
s16._get_prev_close("Q1", 10_000)
check("두 번째 호출은 캐시 사용(REST 추가 0콜)", s16.api.basic_calls == before)
check("진입 핫패스는 raw 캐시만 읽는다(REST 0콜 유지)",
      "self._opening_prices.get(stock_code, 0.0)" in
      _insp.getsource(SM.StrategyManager._maybe_tick_entry))

# ═════════════════════════════════════════════════════════
print("\n[10] 손실청산 후 재진입 — 더 타이트한 대금")
# ═════════════════════════════════════════════════════════
from datetime import timedelta as _td
check("재매수 완화 상수", SM.REBUY_AFTER_LOSS_ENABLED is True
      and SM.REBUY_AFTER_LOSS_WAIT_SEC == 3600.0
      and SM.REBUY_BURST_VALUE_MULT == 2.5)
# (2026-08-05 사양 변경) 예전엔 "항상 1.6억"이라는 절대값이었지만, 주가 스케일
# 도입으로 재매수 문턱도 주가를 따라 움직인다. 검증할 것은 특정 금액이 아니라
# **일반 진입 대비 배수**다(수치를 박으면 상수를 바꿀 때 테스트가 거짓말을 한다).
check("재매수 문턱은 일반 진입의 REBUY_BURST_VALUE_MULT배",
      abs(SM.PHASE1A_BURST_TRADE_VALUE * SM.REBUY_BURST_VALUE_MULT
          - SM.PHASE1A_BURST_TRADE_VALUE * 2.5) < 1)
check("재매수 배수는 1.0 초과(= 일반보다 반드시 엄격)",
      SM.REBUY_BURST_VALUE_MULT > 1.0, str(SM.REBUY_BURST_VALUE_MULT))
# 주가 스케일이 재매수에도 곱셈으로 합성되는지 (저가주 재매수가 조용히
# 완화되지 않는지 — 2.0 유지 시 저가주 문턱이 1.6억 -> 0.66억이 됐다)
_ref = SM.PHASE1A_BURST_TRADE_VALUE * SM.REBUY_BURST_VALUE_MULT
check("재매수 문턱도 주가 스케일을 탄다(저가주가 더 낮음)",
      _ref * SM.burst_price_scale(2_000) < _ref * SM.burst_price_scale(20_000))
check("저가주 재매수 문턱이 구버전(2.0배 고정)보다 낮아지지 않음",
      SM.PHASE1A_BURST_TRADE_VALUE * SM.REBUY_BURST_VALUE_MULT
      * SM.burst_price_scale(2_000)
      >= SM.PHASE1A_BURST_TRADE_VALUE * 2.0 * SM.burst_price_scale(2_000))

def rb(code, minutes_ago, each_value, n=2):
    s, clock = build()
    s._stoploss_blocked.add(code)
    s.sold_at[code] = clock() - _td(minutes=minutes_ago)
    s.phase1b.start_watching(code)
    tf = s.phase1b.trade_flow
    now = time.time()
    for i in range(40):
        tf.add_tick(code, 10_000, "buy", 1, now=now - 110 + i)
    for i in range(n):
        tf.add_tick(code, 10_000, "buy", int(each_value // 10_000), now=now - 2 + i * 0.3)
    return s

BIG = SM.PHASE1A_BURST_TRADE_VALUE * SM.REBUY_BURST_VALUE_MULT
s17 = rb("K1", 90, BIG)
blocked, why = s17._is_rebuy_blocked("K1")
check("60분 경과 + 2배 대금 -> 재진입 허용", not blocked, why)

s18 = rb("K2", 30, BIG)
b2, w2 = s18._is_rebuy_blocked("K2")
check("60분 미경과 -> 차단 유지", b2 and "분" in w2, w2)

s19 = rb("K3", 90, SM.PHASE1A_BURST_TRADE_VALUE)   # 일반 문턱(2배 미달)
b3, w3 = s19._is_rebuy_blocked("K3")
check("일반 문턱은 통과해도 재매수 기준엔 미달 -> 차단", b3, w3)

s20 = rb("K4", 90, BIG)
s20._rebuy_after_loss_used["K4"] = True
b4, w4 = s20._is_rebuy_blocked("K4")
check("종목당 1회 소진 -> 차단", b4 and "1회" in w4, w4)

# 상대 경로 금지 + (2026-08-05) 상대 하한이 절대문턱으로 올라간 뒤의 동작.
# 일반 문턱은 통과하지만 재매수 배수에는 못 미치는 크기를 넣는다.
s21, clock21 = build()
s21._stoploss_blocked.add("K5")
s21.sold_at["K5"] = clock21() - _td(minutes=90)
s21.phase1b.start_watching("K5")
tf21 = s21.phase1b.trade_flow
now21 = time.time()
_each = SM.PHASE1A_BURST_TRADE_VALUE          # 딱 일반 문턱(주가 10,000 -> 계수 1.0)
for i in range(40):
    tf21.add_tick("K5", 10_000, "buy", 1, now=now21 - 110 + i)      # 평균 1틱 = 1만원
for i in range(2):
    tf21.add_tick("K5", 10_000, "buy", int(_each // 10_000), now=now21 - 2 + i*0.3)
ok_norm, _ = s21.check_burst("K5", allow_relative=True)
ok_rebuy, _ = s21.check_burst("K5", allow_relative=False,
                              value_mult=SM.REBUY_BURST_VALUE_MULT)
check("일반 진입 문턱은 통과", ok_norm)
check("재매수 배수(x2.5)에는 미달 -> 차단", not ok_rebuy)
# 평균 1틱이 1만원이라 상대 경로(x20 = 20만)는 원래 아주 헐거웠는데,
# 하한이 절대문턱으로 올라간 뒤로는 상대가 절대보다 헐거울 수 없다.
_, d21 = s21.check_burst("K5", allow_relative=True)
check("상대 하한이 절대문턱 이상(저가주 뒷문 차단)",
      d21.get("rel_min", 0) >= d21.get("burst_min", 0),
      f"rel_min={d21.get('rel_min')} burst_min={d21.get('burst_min')}")
b5, w5 = s21._is_rebuy_blocked("K5")
check("따라서 재진입도 차단", b5, w5)

check("일반 진입의 버스트 판정은 그대로(기본값 1.0/True)",
      "value_mult: float = 1.0" in _insp.getsource(SM.StrategyManager.check_burst)
      and "allow_relative: bool = True" in _insp.getsource(SM.StrategyManager.check_burst))


# ═════════════════════════════════════════════════════════
print("\n[11] 버스트 주가 스케일 (2026-08-05 신규)")
# ═════════════════════════════════════════════════════════
# [배경] 문턱이 전 종목 4천만 고정이라 주가가 사실상 진입 경로를 결정했다.
# 틱 아카이브 107 종목·일 실측: 절대 경로 발생이 저가주 0.05회 vs 고가주
# 6.18회(124배). 실거래 56건에서도 2,500원 미만은 100% '상대' 경로였다.
ps = SM.burst_price_scale

check("기준가(10,000원)에서 계수 1.0 — 기존 값이 그대로 유지되는 지점",
      abs(ps(SM.BURST_PRICE_REF) - 1.0) < 1e-9, str(ps(SM.BURST_PRICE_REF)))
check("주가가 오르면 문턱도 오른다(단조증가)",
      all(ps(a) <= ps(b) for a, b in zip(
          [1_000, 2_000, 5_000, 10_000, 20_000, 50_000],
          [2_000, 5_000, 10_000, 20_000, 50_000, 150_000])))
check("저가주는 계수 < 1 (문턱이 내려감)", ps(2_000) < 1.0, f"{ps(2_000):.3f}")
check("고가주는 계수 > 1 (문턱이 올라감)", ps(30_000) > 1.0, f"{ps(30_000):.3f}")
check("하한 클램프", ps(10) == SM.BURST_PRICE_MIN, str(ps(10)))
check("상한 클램프", ps(1_000_000) == SM.BURST_PRICE_MAX, str(ps(1_000_000)))
check("클램프 범위가 뒤집히지 않음", 0 < SM.BURST_PRICE_MIN < 1.0 < SM.BURST_PRICE_MAX)
# (2026-08-05 저녁) MAX 2.5 -> 2.0. 상한이 **어느 주가부터 걸리는지**를 같이
# 못박는다 — MAX만 보고 "고가주가 완화됐다"고 오해하기 쉽다. 실제 클램프
# 시작점은 10,000 x MAX^(1/ALPHA)이고, 그 아래 종목은 아무 영향을 안 받는다.
_bind = 10_000 * SM.BURST_PRICE_MAX ** (1 / SM.BURST_PRICE_ALPHA)
check("상한 클램프 시작 주가가 3만원대", 30_000 <= _bind <= 40_000, f"{_bind:,.0f}원")
check("클램프 미만 주가는 상한의 영향을 받지 않음",
      ps(_bind * 0.9) < SM.BURST_PRICE_MAX and ps(_bind * 1.1) == SM.BURST_PRICE_MAX)
# 08-05에 실제로 발화가 지연된 두 종목은 클램프 아래라 이 변경의 대상이 아니다.
# (이 사실을 테스트로 박아둬야 "MAX 낮췄으니 해결됐다"는 오해가 안 생긴다)
check("[문서화] 마키나락스 25,900 / GS건설 29,750은 클램프 미적용 구간",
      ps(25_900) < SM.BURST_PRICE_MAX and ps(29_750) < SM.BURST_PRICE_MAX,
      f"x{ps(25_900):.2f} / x{ps(29_750):.2f}")
# 가격을 모르면 현행 동작으로 수렴 — '모름'이 매수를 막지도 열어주지도 않는다
check("가격 0/None/문자 -> 계수 1.0 (현행 수렴)",
      ps(0) == 1.0 and ps(None) == 1.0 and ps(-5) == 1.0 and ps("x") == 1.0)
check("ALPHA=0이면 기능 무효화(전 종목 1.0)로 롤백 가능",
      "BURST_PRICE_ALPHA == 0.0" in _insp.getsource(SM.burst_price_scale))
# 하한이 '대량체결'이라 부를 수 있는 최소 금액은 되는가
check("하한 적용 시에도 절대문턱이 1천만원 이상",
      SM.PHASE1A_BURST_TRADE_VALUE * SM.BURST_PRICE_MIN >= 10_000_000,
      f"{SM.PHASE1A_BURST_TRADE_VALUE * SM.BURST_PRICE_MIN:,.0f}원")

# check_burst가 실제로 계수를 태우는지 (상수만 맞고 배선이 끊긴 경우 방지 —
# 이 코드베이스에서 실제로 여러 번 났던 사고 유형이다)
def burst_at(price, each_value, n=2):
    s, _clk = build()
    s.phase1b.start_watching("PX")
    tf = s.phase1b.trade_flow
    nw = time.time()
    for i in range(40):
        tf.add_tick("PX", price, "buy", max(1, int(200_000 // price)), now=nw - 110 + i)
    for i in range(n):
        tf.add_tick("PX", price, "buy", int(each_value // price), now=nw - 2 + i * 0.3)
    return s.check_burst("PX", now=nw)

_ok_lo, _d_lo = burst_at(2_000, SM.PHASE1A_BURST_TRADE_VALUE * 0.5)
check("저가주(2,000원): 4천만 미달(2천만)이어도 통과 — 구버전은 탈락",
      _ok_lo, f"burst_min={_d_lo.get('burst_min'):,.0f} pmul={_d_lo.get('price_mult')}")
check("저가주 문턱이 실제로 내려갔는지", _d_lo.get("burst_min", 0) < SM.PHASE1A_BURST_TRADE_VALUE)

_ok_hi, _d_hi = burst_at(30_000, SM.PHASE1A_BURST_TRADE_VALUE)
check("고가주(30,000원): 4천만 딱 맞춰선 탈락 — 구버전은 통과",
      not _ok_hi, f"burst_min={_d_hi.get('burst_min'):,.0f} pmul={_d_hi.get('price_mult')}")
_ok_hi2, _ = burst_at(30_000, SM.PHASE1A_BURST_TRADE_VALUE * 2.0)
check("고가주도 스케일된 문턱을 넘으면 통과", _ok_hi2)

check("detail에 주가 계수가 기록돼 사후 추적 가능",
      "price_mult" in _d_hi and "last_price" in _d_hi)


# ═════════════════════════════════════════════════════════
print("\n[12] 정적VI 상단 근접 확정매도 (2026-08-05 신규)")
# ═════════════════════════════════════════════════════════
# [배경] VI 발동 시 2분간 단일가매매 -> 시장가 매도 불가. 익절 캡 미달 상태로
# 갇히면 해제 후 밀려 나온다. 08-05 실측 4/20건이 상단 3% 이내로 근접했고
# 마키나락스는 0.1%까지 붙었다. 반대로 **하단은 20건 중 0건** 근접(손절이
# 항상 먼저) — 그래서 하단 로직은 만들지 않았다.

def vi_setup(open_price, buy, qty=100, warm=False):
    s, clk = build()
    p = put_pos(s, "VI1", buy=buy, qty=qty, warm=warm)
    s._opening_prices["VI1"] = open_price
    return s, p

# 산출 자체
_s, _ = vi_setup(10_000, 10_400)
check("VI 상단 = 시가 x (1+비율)",
      abs(_s.vi_upper_price("VI1") - 10_000 * (1 + SM.VI_STATIC_RATIO)) < 1e-6,
      f"{_s.vi_upper_price('VI1'):,.0f}")
check("시가 캐시가 없으면 0.0 (기능이 쉰다)", _s.vi_upper_price("없는종목") == 0.0)
_s2, _ = vi_setup(0, 10_400)
check("시가가 0이면 0.0", _s2.vi_upper_price("VI1") == 0.0)
# 전일종가로 폴백하지 않는지 — 갭상승 날 조기매도를 막는 핵심 가드
_s3, _ = vi_setup(0, 10_400)
_s3._prev_closes["VI1"] = 9_000
check("전일종가로 폴백하지 않음(갭상승 조기매도 방지)",
      _s3.vi_upper_price("VI1") == 0.0)

# 거리 판정 밴드
_s4, _ = vi_setup(10_000, 10_000)      # VI 상단 11,000
g_far = _s4.vi_upper_gap("VI1", 10_500)
g_near = _s4.vi_upper_gap("VI1", 10_960)     # 0.36% 남음
g_over = _s4.vi_upper_gap("VI1", 11_050)     # 이미 넘음
check("멀면 near=False", g_far and not g_far["near"], f"{g_far['gap_pct']*100:.2f}%")
check("0.5% 이내면 near=True", g_near and g_near["near"], f"{g_near['gap_pct']*100:.2f}%")
check("⚠️ VI선을 이미 넘었으면 None (기준가 갱신 가능성 — 놓치는 게 안전)",
      g_over is None)
check("가격이 0/음수/문자면 None",
      _s4.vi_upper_gap("VI1", 0) is None and _s4.vi_upper_gap("VI1", -1) is None
      and _s4.vi_upper_gap("VI1", "x") is None)
# 2호가 조건은 전 가격대에서 0.5%보다 좁다 -> OR에서 항상 0.5%가 먼저 걸린다.
# (버그가 아니라 설계 결과. pct를 0.2% 아래로 조이면 그때 호가 조건이 살아난다.)
from utils.price_helper import add_ticks as _at
check("[문서화] 2호가 폭은 전 가격대에서 0.5% 미만",
      all((_at(p, SM.VI_UPPER_MARGIN_TICKS) - p) / p < SM.VI_UPPER_MARGIN_PCT
          for p in (1500, 3000, 9000, 25000, 80000, 150000)))

# 실제 매도가 나가는가 / 안 나가는가
def vi_run(open_price, buy, price, warm=False, guard=False, loss_guard=None):
    s, p = vi_setup(open_price, buy, warm=warm)
    if guard:
        s._is_index_guard_active = lambda now_dt=None: True
    s.on_price_update("VI1", price)
    return s

_r = vi_run(10_000, 10_000, 10_960)
check("근접 + 순이익>0 -> 확정매도 실행", "VI1" not in _r.holdings)
check("사유에 'VI 상단 확정매도'가 남는다",
      any("VI 상단 확정매도" in (x.get("exit_reason") or "") for x in _Repo.sells),
      str([x.get("exit_reason") for x in _Repo.sells])[:90])

# ⚠️ 손실 구간에서는 절대 발동하지 않아야 한다 (08-03 결함과 같은 원칙)
_r = vi_run(10_000, 11_000, 10_960)          # 매수 11,000 -> 현재 10,960 = 손실
check("🔴 순손실이면 VI 매도 안 함(손절/본전스톱 담당)", "VI1" in _r.holdings)
# 순이익이 수수료 미만(순<=0)인 경계도 막혀야 한다
_r = vi_run(10_000, 10_950, 10_960)          # 총 +0.09% < 수수료 0.23%
check("🔴 순이익 0 이하(수수료 미만)면 발동 안 함", "VI1" in _r.holdings)

# ⚠️ 아래 두 케이스는 **익절 캡(4.0%)이 먼저 발동**해서 포지션이 사라진다.
#    그러니 "보유가 남아있는지"가 아니라 **"VI 사유로 나가지 않았는지"**를 봐야
#    한다(처음엔 보유 여부로 단언했다가 이 정상 동작을 실패로 잡았다).
def vi_reason(open_price, buy, price):
    vi_run(open_price, buy, price)
    return " | ".join((x.get("exit_reason") or "") for x in _Repo.sells)

_why = vi_reason(10_000, 10_000, 10_500)     # VI까지 4.76% — 멀다
check("멀면 VI 사유로 안 나간다(익절캡이 담당)",
      "VI 상단" not in _why and "익절" in _why, _why[:70])
_why = vi_reason(10_000, 10_000, 11_050)     # 이미 VI선 초과
check("VI선 초과면 VI 사유로 안 나간다",
      "VI 상단" not in _why, _why[:70])
# 캡이 아직 멀고 VI만 가까운 조합 — 이때가 이 기능의 존재 이유다
_why = vi_reason(10_800, 11_500, 11_840)     # 매수 11,500 / VI 11,880 / +2.96%
check("캡(4%) 미달인데 VI만 가까우면 VI가 판다",
      "VI 상단" in _why, _why[:80])

# 워밍업 중에도 작동 (가격 기반이라 안전)
_r = vi_run(10_000, 10_000, 10_960, warm=True)
check("워밍업 중에도 VI 매도는 작동", "VI1" not in _r.holdings)

# 우선순위: 손절 > 지수가드 > VI
_s, _p = vi_setup(10_000, 11_500)            # VI 상단 11,000, 매수 11,500
_s.on_price_update("VI1", 11_155)            # -3.0% 손절 & VI 0.5% 이내는 아님
check("손절 구간에서는 손절 사유로 나간다",
      "VI1" not in _s.holdings
      and any("손절" in (x.get("exit_reason") or "") for x in _Repo.sells),
      str([x.get("exit_reason") for x in _Repo.sells])[:70])

_s, _p = vi_setup(10_000, 10_000)
_s._is_index_guard_active = lambda now_dt=None: True
_s._now = lambda: datetime(2026, 8, 5, 11, 10, 0)
_s.on_price_update("VI1", 10_960)
check("지수가드 발동 중엔 가드 사유가 우선",
      "VI1" not in _s.holdings
      and any("지수 가드" in (x.get("exit_reason") or "") for x in _Repo.sells),
      str([x.get("exit_reason") for x in _Repo.sells])[:70])

# 예외 안전성 — 판정이 터져도 매매는 계속돼야 한다
_s, _p = vi_setup(10_000, 10_000)
_s.vi_upper_gap = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    _s.on_price_update("VI1", 10_400)
    check("VI 판정 예외가 나도 on_price_update가 죽지 않음", True)
except Exception as e:
    check("VI 판정 예외가 나도 on_price_update가 죽지 않음", False, str(e))
check("예외 시에는 매도하지 않음(보수적)", "VI1" in _s.holdings)

# 스위치
check("VI_UPPER_EXIT_ENABLED=False로 완전 무력화 가능",
      "VI_UPPER_EXIT_ENABLED and net_rate > 0"
      in _insp.getsource(SM.StrategyManager.on_price_update))


# ═════════════════════════════════════════════════════════
print("\n[13] VI 상단 근접 매수차단 (2026-08-06 신규)")
# ═════════════════════════════════════════════════════════
# [왜] 무장·버스트가 다 맞아도 VI 상단 코앞에서 사면 (a) VI 발동 시 2분간
# 손도 못 대고 (b) 익절 캡까지 갈 공간이 없고 (c) 매수 직후 VI 확정매도(0.5%)에
# 걸려 수수료만 내고 되판다. -> 상단까지 3% 이하로 붙으면 매수하지 않는다.

check("매수차단 상수", SM.VI_UPPER_ENTRY_BLOCK_ENABLED is True
      and SM.VI_UPPER_ENTRY_BLOCK_PCT == 0.03,
      f"{SM.VI_UPPER_ENTRY_BLOCK_ENABLED} / {SM.VI_UPPER_ENTRY_BLOCK_PCT}")
check("매수차단 폭 > 매도발동 폭 (밴드가 뒤집히면 사자마자 되판다)",
      SM.VI_UPPER_ENTRY_BLOCK_PCT > SM.VI_UPPER_MARGIN_PCT,
      f"매수차단 {SM.VI_UPPER_ENTRY_BLOCK_PCT} vs 매도 {SM.VI_UPPER_MARGIN_PCT}")
check("매수차단 폭 < 정적VI 폭", SM.VI_UPPER_ENTRY_BLOCK_PCT < SM.VI_STATIC_RATIO)

def vb(open_px, price, code="VB"):
    s, _ = build()
    if open_px:
        s._opening_prices[code] = open_px
    return s.vi_entry_block_reason(code, price)

# 시가 10,000 -> VI 상단 11,000 -> 차단 시작 11,000/1.03 = 10,680원
check("여유 충분(3% 초과)하면 차단 안 함", vb(10_000, 10_600) is None,
      str(vb(10_000, 10_600)))
# 경계는 '정확히 3%'가 아니라 **양옆**으로 본다 — 부동소수점 때문에 정확히
# 3.000%인 가격은 계산 순서에 따라 위/아래로 갈린다(테스트가 그걸 잡아냈다).
_edge = 11_000 / (1 + SM.VI_UPPER_ENTRY_BLOCK_PCT)     # 여유가 딱 3%인 가격
check("경계 바로 아래(여유 3.01%)는 통과", vb(10_000, _edge * 0.9999) is None,
      f"{_edge*0.9999:,.1f}원")
check("경계 바로 위(여유 2.99%)는 차단", vb(10_000, _edge * 1.0001) is not None,
      f"{_edge*1.0001:,.1f}원")
check("3% 이내면 차단", vb(10_000, 10_800) is not None, str(vb(10_000, 10_800))[:60])
check("VI선 초과도 차단(이미 발동했을 수 있음)", vb(10_000, 11_500) is not None)
check("차단 사유 문구에 'VI 상단 근접' 포함",
      "VI 상단 근접" in (vb(10_000, 10_900) or ""), str(vb(10_000, 10_900))[:70])

# ⚠️ 시가를 모르면 차단하지 않는다 — 막으면 하루 종일 매수 0건이 되는데
#    로그를 뒤지기 전엔 안 보인다(08-05 시가대비 필터가 정확히 그랬다).
check("🔴 시가 캐시가 없으면 차단하지 않음(전면 매수정지 방지)",
      vb(None, 10_900) is None)
check("가격이 0/음수/문자면 차단하지 않음",
      vb(10_000, 0) is None and vb(10_000, -1) is None and vb(10_000, "x") is None)
check("VI_UPPER_ENTRY_BLOCK_ENABLED=False면 완전 무력화",
      "if not VI_UPPER_ENTRY_BLOCK_ENABLED"
      in _insp.getsource(SM.StrategyManager.vi_entry_block_reason))

# 차단 시작 지점이 '시가 대비 +6.80%'인지 (문서·주석과 일치해야 한다)
_start = (1 + SM.VI_STATIC_RATIO) / (1 + SM.VI_UPPER_ENTRY_BLOCK_PCT)
check("차단 시작 = 시가 대비 +6.80%", abs(_start - 1.0680) < 0.0005,
      f"시가 x{_start:.4f}")

# ── 실제 진입 경로 두 곳에서 막히는가 (규칙 복제 사고 방지) ──
_src_plan = _insp.getsource(SM.StrategyManager._open_entry_plan)
_src_buy = _insp.getsource(SM.StrategyManager._execute_buy)
check("계획 생성 경로에 매수차단 배선", "vi_entry_block_reason" in _src_plan)
check("주문 직전 경로에 매수차단 배선", "vi_entry_block_reason" in _src_buy)
check("판정이 단일 함수(vi_entry_block_reason)로 모여 있다",
      _src_plan.count("VI_UPPER_ENTRY_BLOCK_PCT") == 0
      and _src_buy.count("VI_UPPER_ENTRY_BLOCK_PCT") == 0,
      "호출부가 상수를 직접 보면 규칙이 갈라진다")

# 계획이 아예 안 걸리는지 (슬롯 점유 방지)
s_p, _ = build()
s_p._opening_prices["VP"] = 10_000
s_p._open_entry_plan("VP", "VP", "1A", {}, "1A", "주도주상위", 10_900)
check("🔴 VI 근접 종목엔 되돌림 계획을 걸지 않는다(슬롯 점유 방지)",
      "VP" not in s_p._entry_plans)
s_p2, _ = build()
s_p2._opening_prices["VP"] = 10_000
s_p2._open_entry_plan("VP", "VP", "1A", {}, "1A", "주도주상위", 10_500)
check("✅ 대조군: 여유 있으면 계획이 정상 생성", "VP" in s_p2._entry_plans)

# 주문 직전 하드가드 — 실제로 매수가 안 나가는가
def try_buy(price, open_px=10_000, code="VE"):
    s, _ = build()
    s._opening_prices[code] = open_px
    s._prev_closes[code] = 10_000
    s.phase1b.start_watching(code)
    s._execute_buy(code, code, "1A", {"current_price": price}, "1A")
    return s

_s = try_buy(10_900)          # VI 상단 11,000까지 0.92% — 차단돼야 한다
check("🔴 VI 근접이면 주문이 나가지 않는다",
      not _s.order_manager.orders and "VE" not in _s.holdings,
      str(_s.order_manager.orders)[:60])
_s = try_buy(10_400)          # 여유 5.77% — 정상 매수돼야 한다
check("✅ 대조군: 여유 있으면 주문이 정상 실행",
      bool(_s.order_manager.orders) or "VE" in _s.holdings,
      str(_s.order_manager.orders)[:60])
# 시가를 모르면 주문이 막히지 않아야 한다(전면 매수정지 방지)
_s2, _ = build()
_s2._prev_closes["VF"] = 10_000
_s2.phase1b.start_watching("VF")
_s2._execute_buy("VF", "VF", "1A", {"current_price": 10_900}, "1A")
check("🔴 시가 미상이면 주문이 막히지 않는다",
      bool(_s2.order_manager.orders) or "VF" in _s2.holdings,
      str(_s2.order_manager.orders)[:60])

# 진단 분류
check("탈락 사유가 '기타'로 뭉개지지 않는다",
      any("VI 상단 근접" in r[0] for r in SM.StrategyManager._REJECT_RULES))
check("진단 라벨에 수치를 박지 않았다",
      not any(ch.isdigit() for r in SM.StrategyManager._REJECT_RULES
              if "VI 상단 근접" in r[0] for ch in r[0]))

print("\n" + "=" * 62)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건   ({time.time() - T0:.1f}초)")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
