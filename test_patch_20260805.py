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

print("\n" + "=" * 62)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건   ({time.time() - T0:.1f}초)")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
