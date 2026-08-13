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
    # ⚠️ 실물은 `update_sell(trade_id, ...)`로 **trade_id가 위치 인자**다. 키워드 전용으로
    # 두면 실물이 정상인데 스텁이 TypeError를 내고, 호출부의 except가 그걸 삼켜
    # **감사가 조용히 거짓말한다**(2026-08-10에 실제로 밟은 함정).
    def update_sell(cls, trade_id=None, **kw):
        cls.sells.append({"trade_id": trade_id, **kw}); return True
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
# 🔴 (2026-08-13) 바닥 0.002 -> 0.010. 08-05에 넣은 "슬리피지만큼 위"는
#    유지되고(여전히 0보다 크다) 값만 +1% 확보로 올라갔다.
check("② 본전스톱 바닥 +1.0% (08-13 상향, 구 +0.2%)",
      abs(SM.BREAKEVEN_FLOOR - 0.010) < 1e-9, f"{SM.BREAKEVEN_FLOOR}")
check("②-b 바닥은 여전히 0보다 크다(시장가 슬리피지 방어 — 08-05 취지 유지)",
      SM.BREAKEVEN_FLOOR > 0)
check("④ 1A 등락률 13% / 눌림 10%",
      SM.MAX_ENTRY_CHANGE_PCT == 13.0 and SM.MAX_ENTRY_CHANGE_PCT_PULLBACK == 10.0)
check("⑤ 되돌림 -0.3%/-0.7%",
      SM.ENTRY_PULLBACK_TRANCHES == ((0.003, 0.5), (0.007, 0.5)))
# (2026-08-06 사양 변경) 상승 이탈은 OFF. 폭 상수는 보존한다 — 되살릴 때
# 값까지 다시 정하지 않아도 되게. 상세 근거는 test_patch_20260806.py [5].
check("⑦ 상승 이탈 OFF (폭 상수 0.3%는 보존)",
      SM.ENTRY_BREAKOUT_ENABLED is False and abs(SM.ENTRY_BREAKOUT_PCT - 0.003) < 1e-9)
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
# 🔴 (2026-08-11) -3% 물타기가 손절선(-4.5%)보다 **먼저** 개입해 평단을
#    낮추므로, rescue 경로를 보는 [2]~[7] 구간에서는 꺼둔다.
#    물타기 자체의 검증은 test_patch_20260811.py에 따로 있다.
_SV_AVGDOWN = SM.AVG_DOWN_ENABLED
SM.AVG_DOWN_ENABLED = False
print("\n[2] #3 추가매수 — 관찰 창 -> 반등 확증 시에만 발동")
# ═════════════════════════════════════════════════════════
# ⚠️ 손절은 -3%에 **최초로 닿는 순간** 발동하므로 그 시점엔 현재가=최근저점이라
#    "저점 대비 +0.3% 반등"이 정의상 0%다. 그래서 ①②가 성립하면 매도를 잠시
#    보류하고(관찰 창) 그 안에서 반등을 확인한다. 이 순서를 그대로 재현한다.
s, _ = build()
pos = put_pos(s)
feed(s, "R1")
s.on_price_update("R1", 9_540)          # 손절선(-4.5%) 첫 이탈
sells = [o for o in s.order_manager.orders if o["side"] == "sell"]
check("①② 충족 -> 첫 도달에 매도하지 않고 관찰 시작",
      not sells and pos.get("rescue_watch_until") is not None,
      f"매도 {len(sells)}건")
check("관찰 저점이 기록됨", pos.get("rescue_low") == 9_540)

s.on_price_update("R1", 9_500)          # 더 밀림 — 저점 갱신, 아직 반등 아님
check("관찰 중 저점 갱신", pos.get("rescue_low") == 9_500)
check("아직 매도도 매수도 없음",
      not [o for o in s.order_manager.orders if o["side"] in ("sell", "buy")])

s.on_price_update("R1", 9_540)          # 저점 대비 +0.42%, 여전히 손절선 아래
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
    s2.on_price_update("R2", 9_540)
    sold = [o for o in s2.order_manager.orders if o["side"] == "sell"]
    check(f"①② 중 {nm} -> 관찰도 안 하고 즉시 손절",
          len(sold) == 1 and "R2" not in s2.holdings, f"매도 {len(sold)}건")

# ═════════════════════════════════════════════════════════
print("\n[3] #3 관찰 창의 출구 — 하한 이탈 / 만료 / 반등 없음")
# ═════════════════════════════════════════════════════════
sA, _ = build()
pA = put_pos(sA, "RA")
feed(sA, "RA")
sA.on_price_update("RA", 9_540)
check("관찰 시작됨", pA.get("rescue_watch_until") is not None)
sA.on_price_update("RA", 9_440)   # 원가 -5.6% (관찰 하한 -5.5% 이탈)
soldA = [o for o in sA.order_manager.orders if o["side"] == "sell"]
check("관찰 중 하한 이탈 -> 즉시 손절",
      len(soldA) == 1 and "RA" not in sA.holdings)

sB, _ = build()
pB = put_pos(sB, "RB")
feed(sB, "RB")
sB.on_price_update("RB", 9_540)
pB["rescue_watch_until"] = time.time() - 1     # 창 만료 상태로
sB.on_price_update("RB", 9_545)
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
s4.on_price_update("R4", 9_540)
check("하루 한도 소진 -> 관찰 없이 손절",
      len([o for o in s4.order_manager.orders if o["side"] == "sell"]) == 1
      and "R4" not in s4.holdings)

s5, _ = build()
put_pos(s5, "R5")
feed(s5, "R5")
s5._entry_block_reason = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
s5.on_price_update("R5", 9_540)
check("판정 중 예외 -> 손절로 수렴 (최후 방어선 유지)",
      len([o for o in s5.order_manager.orders if o["side"] == "sell"]) == 1
      and "R5" not in s5.holdings)

s6, _ = build()
put_pos(s6, "R6")
feed(s6, "R6")
s6._entry_block_reason = lambda: "MDD 일손실 차단"
s6.on_price_update("R6", 9_540)
check("MDD/가드 차단 중 -> 관찰 없이 손절",
      len([o for o in s6.order_manager.orders if o["side"] == "sell"]) == 1)

s3, _ = build()
p3 = put_pos(s3, "R3")
feed(s3, "R3")
s3.on_price_update("R3", 9_540)
s3.on_price_update("R3", 9_500)
s3.on_price_update("R3", 9_540)
check("1회차 추가매수 성공", p3.get("rescue_added") is True)
s3.order_manager.orders.clear()
s3.on_price_update("R3", 9_410)   # 원가 -5.9% (최종선 -6% 전)
check("같은 종목 2회차 추가매수는 없음",
      not [o for o in s3.order_manager.orders if o["side"] == "buy"])

# ═════════════════════════════════════════════════════════
print("\n[5] #3 최종 방어선 — 원가 -6%면 무조건 청산")
# ═════════════════════════════════════════════════════════
s7, _ = build()
p7 = put_pos(s7, "R7")
feed(s7, "R7")
s7.on_price_update("R7", 9_540)
s7.on_price_update("R7", 9_500)
s7.on_price_update("R7", 9_540)
check("추가매수 완료", p7.get("rescue_added") is True)
s7.order_manager.orders.clear()
s7.on_price_update("R7", 9_410)   # 원가 -5.9% (아직 -6% 전)
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

# ⚠️ (2026-08-06 사양 변경) 상승 이탈은 **OFF**로 전환됐다.
# 08-06 실거래에서 이 경로가 되돌림보다 뚜렷이 나빴다:
#   상승이탈 n=9 평균 -1.00% 승률 22% 손절 4건 / 되돌림 n=27 +0.23% 41%
# 아래 단언은 옛 사양(ON)을 검증하던 것을 새 사양(OFF) 기준으로 교체한 것이다.
# 로직 자체는 보존돼 있어 ENTRY_BREAKOUT_ENABLED=True면 다시 옛 동작이 된다.
check("[사양] 상승 이탈 OFF", SM.ENTRY_BREAKOUT_ENABLED is False)

s8, _ = build()
make_plan(s8)
check("계획 생성됨", "B1" in s8._entry_plans)
s8._try_fill_entry_plan("B1", 10_029, now=time.time())    # +0.29%
check("+0.29%로는 발동하지 않음(경계 아래)",
      "B1" not in s8.holdings and "B1" in s8._entry_plans)
s8._try_fill_entry_plan("B1", 10_030, now=time.time())    # +0.30%
check("[신사양] +0.30% 돌파해도 즉시 체결하지 않는다(추격매수 차단)",
      "B1" not in s8.holdings, str(list(s8.holdings)))
check("[신사양] 계획은 유지 — 되돌림을 계속 기다린다", "B1" in s8._entry_plans)
s8._try_fill_entry_plan("B1", 10_100, now=time.time())    # +1.0%
check("[신사양] 더 크게 올라도 마찬가지", "B1" not in s8.holdings)

# 로직 보존 확인 — 플래그만 되살리면 옛 동작이 그대로 나온다
_saved = SM.ENTRY_BREAKOUT_ENABLED
try:
    SM.ENTRY_BREAKOUT_ENABLED = True
    s8b, _ = build()
    make_plan(s8b, "B1B")
    s8b._try_fill_entry_plan("B1B", 10_030, now=time.time())
    check("[보존] 플래그를 켜면 +0.30%에 전량 체결(옛 동작 그대로)",
          "B1B" in s8b.holdings and "B1B" not in s8b._entry_plans)

    s9, _ = build()
    make_plan(s9, "B2")
    s9.api.get_stock_change_rate = lambda c: 20.0   # 등락률 20% (1A 상한 13% 초과)
    s9.api.get_basic_quote = lambda c: {"change_rate": 20.0}
    s9._prev_closes = {}
    s9._try_fill_entry_plan("B2", 10_030, now=time.time())
    check("[보존] 상승 이탈해도 등락률 상한 초과면 미집행",
          "B2" not in s9.holdings, str(list(s9.holdings)))
finally:
    SM.ENTRY_BREAKOUT_ENABLED = _saved

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
s13.on_price_update("R8", 9_540)
check("워밍업 중에도 손절은 작동",
      len([o for o in s13.order_manager.orders if o["side"] == "sell"]) == 1)

s14, _ = build()
put_pos(s14, "R9")
feed(s14, "R9")
SM.RESCUE_ADD_ENABLED = False
s14.on_price_update("R9", 9_540)
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
SM.AVG_DOWN_ENABLED = _SV_AVGDOWN     # 물타기 원복 ([2]~[7] 구간 한정)
check("[원복] 물타기 설정이 되돌아왔다", SM.AVG_DOWN_ENABLED is True)
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
check("상한 클램프 시작 주가가 7만원대", 70_000 <= _bind <= 80_000, f"{_bind:,.0f}원")
check("클램프 미만 주가는 상한의 영향을 받지 않음",
      ps(_bind * 0.9) < SM.BURST_PRICE_MAX and ps(_bind * 1.1) == SM.BURST_PRICE_MAX)
# 08-05에 실제로 발화가 지연된 두 종목은 클램프 아래라 이 변경의 대상이 아니다.
# (이 사실을 테스트로 박아둬야 "MAX 낮췄으니 해결됐다"는 오해가 안 생긴다)
# (2026-08-06) MAX 2.0 -> 3.0. 클램프 시작가가 35,264 -> 73,704원으로 올라가
# **10만원대 고가주만** 강화된다. 3만원대는 자연계수라 그대로다 — 08-06에
# 손절난 GS건설(32,600)이 여기 해당하며, 이 변경으로 막히지 않는다(문서화).
check("[문서화] 3만원대는 클램프 밖 — MAX를 올려도 불변",
      ps(32_600) < SM.BURST_PRICE_MAX, f"x{ps(32_600):.2f}")
check("10만원대는 클램프 적용", ps(113_900) == SM.BURST_PRICE_MAX)
check("08-06 승자 구간(2만/7천/6천원)은 클램프 밖 — 영향 없음",
      all(ps(p) < SM.BURST_PRICE_MAX for p in (20_500, 7_380, 6_120)))
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

# (2026-08-12 사양변경) VI 확정매도가 **50% 분할**로 바뀌었다 —
# 실측 9건에서 실현 +2.37%인데 판 뒤 30분 +5.67%, 78%가 +3% 이상 더 갔다.
# 그래서 'holdings에서 사라졌는가'로는 더 이상 판정할 수 없다. 매도 주문이
# 나갔는가 + 잔량이 남았는가로 본다. 분할 상세는 test_patch_20260813 [3].
_r = vi_run(10_000, 10_000, 10_960)
_vi_sold = [o for o in _r.order_manager.orders if o.get("side") == "sell"]
check("근접 + 순이익>0 -> 확정매도 실행", len(_vi_sold) >= 1,
      f"매도 {len(_vi_sold)}건")
check("🆕 전량이 아니라 절반만 나간다 (08-12)",
      bool(_vi_sold) and _vi_sold[0]["qty"] == 50 and "VI1" in _r.holdings,
      f"{_vi_sold[0]['qty'] if _vi_sold else 0}주 / 잔량 "
      f"{_r.holdings.get('VI1', {}).get('qty')}")

# 🔴 롤백 경로 — 끈 기능의 배선을 남겨둔다(08-10 교훈).
_sv_vip = SM.VI_UPPER_EXIT_PARTIAL
SM.VI_UPPER_EXIT_PARTIAL = False
try:
    _r2 = vi_run(10_000, 10_000, 10_960)
    check("롤백(False): 종전대로 전량 매도 + DB 기록",
          "VI1" not in _r2.holdings
          and any("VI 상단 확정매도" in (x.get("exit_reason") or "")
                  for x in _Repo.sells),
          str([x.get("exit_reason") for x in _Repo.sells])[:90])
finally:
    SM.VI_UPPER_EXIT_PARTIAL = _sv_vip

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
    # (2026-08-12) 여기서 보려는 건 **어느 규칙이 판정을 가져가는가**이지
    # 분할 여부가 아니다. VI 분할이 켜져 있으면 update_sell을 안 부르므로
    # (포지션이 안 닫힘) 사유가 DB에 안 남는다 -> 판정 목적에 맞게 끄고 잰다.
    # 분할 동작 자체는 위 블록과 test_patch_20260813 [3]에서 본다.
    _sv = SM.VI_UPPER_EXIT_PARTIAL
    SM.VI_UPPER_EXIT_PARTIAL = False
    try:
        vi_run(open_price, buy, price)
        return " | ".join((x.get("exit_reason") or "") for x in _Repo.sells)
    finally:
        SM.VI_UPPER_EXIT_PARTIAL = _sv

# (2026-08-10) 캡 4.0 -> 6.0 상향으로 10,500(+5%)은 더 이상 캡에 안 닿는다.
# 가격을 **상수에서 역산**해 캡을 확실히 넘긴다(앞으로 캡을 또 바꿔도 따라온다).
_cap_px = int(10_000 * (1 + SM.TAKE_PROFIT_CAP + SM.ROUND_TRIP_COST) + 50)
_why = vi_reason(10_000, 10_000, _cap_px)    # VI(11,000)까지는 아직 멀다
check("멀면 VI 사유로 안 나간다(익절캡이 담당)",
      "VI 상단" not in _why and "익절" in _why, f"{_cap_px} -> {_why[:60]}")
_why = vi_reason(10_000, 10_000, 11_050)     # 이미 VI선 초과
check("VI선 초과면 VI 사유로 안 나간다",
      "VI 상단" not in _why, _why[:70])
# 캡이 아직 멀고 VI만 가까운 조합 — 이때가 이 기능의 존재 이유다
_why = vi_reason(10_800, 11_500, 11_840)     # 매수 11,500 / VI 11,880 / +2.96%
check(f"캡({SM.TAKE_PROFIT_CAP*100:.0f}%) 미달인데 VI만 가까우면 VI가 판다",
      "VI 상단" in _why, _why[:80])

# 워밍업 중에도 작동 (가격 기반이라 안전)
# (2026-08-12) 분할이라 잔량이 남으므로 매도 주문 발생으로 판정한다.
_r = vi_run(10_000, 10_000, 10_960, warm=True)
check("워밍업 중에도 VI 매도는 작동",
      any(o.get("side") == "sell" for o in _r.order_manager.orders),
      f"매도 {sum(1 for o in _r.order_manager.orders if o.get('side') == 'sell')}건")

# 우선순위: 손절 > 지수가드 > VI
_s, _p = vi_setup(10_000, 11_500)            # VI 상단 11,000, 매수 11,500
# (2026-08-11) 손절선이 상수라 수치를 박지 않는다. 물타기(-3%)는 손절 경로를
# 가로채므로 이 판정에서는 끈다(우선순위 검증이 목적).
_sv_ad_vi = SM.AVG_DOWN_ENABLED
SM.AVG_DOWN_ENABLED = False
try:
    _s.on_price_update("VI1", int(11_500 * (1 + SM.STOP_LOSS_RATE)) - 1)
finally:
    SM.AVG_DOWN_ENABLED = _sv_ad_vi
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

# ⚠️ (2026-08-06 사용자 지정) 현재는 **꺼져 있다.** 08-05 소급 결과 이 규칙이
#   막는 2건이 하루 실현금액의 124%였고, PS일렉은 이미 VI가 발동·해제된 뒤라
#   과잉차단이었다. 로직은 보존하고 플래그만 False — 아래 동작 테스트는
#   플래그를 임시로 켜서 돌린다(되살릴 때를 대비해 커버리지를 유지).
check("매수차단 현재 OFF (사용자 지정)", SM.VI_UPPER_ENTRY_BLOCK_ENABLED is False
      and SM.VI_UPPER_ENTRY_BLOCK_PCT == 0.03,
      f"{SM.VI_UPPER_ENTRY_BLOCK_ENABLED} / {SM.VI_UPPER_ENTRY_BLOCK_PCT}")
check("OFF 상태에선 어떤 가격에도 차단하지 않는다",
      build()[0].vi_entry_block_reason("X", 10_900) is None)
SM.VI_UPPER_ENTRY_BLOCK_ENABLED = True     # ↓ 로직 검증용으로 임시 ON
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
SM.VI_UPPER_ENTRY_BLOCK_ENABLED = False    # 임시 ON 해제 — 실제 운영값 복원
check("[정리] 임시 ON을 되돌렸다", SM.VI_UPPER_ENTRY_BLOCK_ENABLED is False)


# ═════════════════════════════════════════════════════════
print("\n[14] 시가대비 완화(8%) + 6% 이상 버스트 강화 (2026-08-06 신규)")
# ═════════════════════════════════════════════════════════
# [배경] 08-05 실매수 20건에 5% 컷을 소급하면 2건이 막히는데 **그 2건이 하루
# 실현금액의 124%**였다(아진엑스텍 +12,980원). -> 8%로 완화하고, 대신 6% 이상
# 구간은 버스트 문턱을 1.5배로 올려 "확실히 큰 손"만 통과시킨다.
check("시가대비 상한 8%", SM.PHASE1A_LEADING_OPEN_SURGE_CAP == 8.0,
      str(SM.PHASE1A_LEADING_OPEN_SURGE_CAP))
check("강화 시작 6% / 배수 1.5",
      SM.PHASE1A_OPEN_SURGE_STRICT_FROM == 6.0
      and SM.PHASE1A_OPEN_SURGE_BURST_MULT == 1.5)
check("강화 시작 < 매수보류 상한 (밴드가 뒤집히지 않음)",
      SM.PHASE1A_OPEN_SURGE_STRICT_FROM < SM.PHASE1A_LEADING_OPEN_SURGE_CAP)
check("강화 배수 > 1.0 (반드시 엄격해지는 방향)",
      SM.PHASE1A_OPEN_SURGE_BURST_MULT > 1.0)

def burst_at_open(open_px, price, each, n=2, code="OS"):
    s, _ = build()
    s._opening_prices[code] = open_px
    s.phase1b.start_watching(code)
    tf = s.phase1b.trade_flow
    nw = time.time()
    for i in range(40):
        tf.add_tick(code, price, "buy", max(1, int(200_000 // price)), now=nw - 110 + i)
    for i in range(n):
        tf.add_tick(code, price, "buy", int(each // price), now=nw - 2 + i * 0.3)
    return s.check_burst(code, now=nw)

_base = SM.PHASE1A_BURST_TRADE_VALUE * SM.burst_price_scale(10_600) * 1.05
_ok, _d = burst_at_open(10_000, 10_600, _base)          # 시가대비 +6% -> 강화
check("🔴 시가대비 6%면 기본 문턱을 넘겨도 탈락(1.5배 요구)", not _ok,
      f"급등x{_d.get('surge_mult')} 문턱 {_d.get('burst_min',0)/10000:,.0f}만")
_ok2, _d2 = burst_at_open(10_000, 10_600,
                          SM.PHASE1A_BURST_TRADE_VALUE
                          * SM.burst_price_scale(10_600) * 1.6)
check("✅ 1.5배를 넘기면 통과", _ok2, f"급등x{_d2.get('surge_mult')}")
_ok3, _d3 = burst_at_open(10_000, 10_300, _base)        # 시가대비 +3% -> 평상
check("✅ 대조군: 시가대비 3%는 평상 문턱(강화 없음)",
      _ok3 and _d3.get("surge_mult") == 1.0, f"급등x{_d3.get('surge_mult')}")
_ok4, _d4 = burst_at_open(0, 10_600, _base)             # 시가 미상
check("🔴 시가를 모르면 강화하지 않는다(현행 수렴)",
      _ok4 and _d4.get("surge_mult") == 1.0, f"급등x{_d4.get('surge_mult')}")
check("detail에 급등 계수가 기록돼 사후 추적 가능", "surge_mult" in _d)
# 경계
_, _e1 = burst_at_open(10_000, 10_599, 1)
_, _e2 = burst_at_open(10_000, 10_601, 1)
check("경계: +5.99%는 평상 / +6.01%는 강화",
      _e1.get("surge_mult") == 1.0 and _e2.get("surge_mult") == 1.5,
      f"{_e1.get('surge_mult')} / {_e2.get('surge_mult')}")

print("\n" + "=" * 62)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건   ({time.time() - T0:.1f}초)")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
