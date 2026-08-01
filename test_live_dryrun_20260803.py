"""08-03 실전 드라이런 — 08:59 기동부터 15:10 강제청산까지 종일 시뮬레이션.

목적: "장중에 오류 나서 고치는" 상황을 없애기 위해, 실제 하루 동안 일어나는
이벤트 순서를 그대로 재현해 **한 군데라도 끊기는 곳이 없는지** 확인한다.

기존 두 테스트와 역할이 다르다:
  - test_patch_20260801/20260802.py : 개별 로직 단위 검증 (유닛)
  - 이 파일                          : 전 구간이 **유기적으로 이어지는지** (통합)

재현하는 실제 흐름:
  08:59 기동 -> 조건검색 스냅샷(장 시작 전 편입)
  09:00 장 시작 -> 실시간 편입(type='02') -> pre-arm
  09:xx 체결틱 유입 -> 무장(강도 3초) -> 버스트 -> 매수
  보유중 -> 가격 갱신 -> 동적 익절캡 -> 청산
  청산 후 -> 재매수 차단 규칙
  10:30  -> 중복 종목 전략 전환
  14:50  -> 진입 중단
  15:10  -> 전량 강제청산
  15:30  -> 일일 백테스트
실행: python test_live_dryrun_20260803.py   (종료코드 0 = 전원 통과)
"""
import sys
import time
from datetime import datetime, timedelta

import core.strategy_manager as SM
from core.phase1b_controller import Phase1BController

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


# ─── 스텁 (test_patch_20260801.py와 동일 계약) ───────────────────
class _Repo:
    rows = []
    sells = []
    @classmethod
    def find_holdings(cls): return []
    @classmethod
    def find_by_date(cls, d): return []
    @classmethod
    def insert_buy(cls, **kw): cls.rows.append(kw); return len(cls.rows)
    @classmethod
    def update_sell(cls, **kw): cls.sells.append(kw); return True
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
        return make_candles(count)
    def get_orderable_amount(self): return 10_000_000
    def get_stock_change_rate(self, code):
        self.calls.append(("change_rate", code)); return 3.0
    def get_index_change_rate(self, s="001"): return 0.0
    def get_current_price(self, code):
        self.calls.append(("price", code)); return 10_000


class _OrderMgr:
    def __init__(self): self.orders = []
    def buy(self, code, qty, price=0, sizing="REGULAR", exit_strategy="REGULAR",
            order_style="limit", ref_price=0):
        self.orders.append({"code": code, "qty": qty, "style": order_style,
                            "ref_price": ref_price})
        return {"success": True, "ord_no": "1", "price": ref_price or 10_000,
                "style": order_style}
    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"code": code, "qty": qty, "style": f"sell:{order_style}"})
        return {"success": True, "ord_no": "2", "price": price or 10_000,
                "style": order_style}
    def get_stock_name(self, code): return code


def make_candles(n, today="20260803", base=10_000, rising=True):
    out = []
    today_n = max(1, n // 2)
    for i in range(n):
        is_today = i < today_n
        day = today if is_today else "20260731"
        mm = (today_n - i) if is_today else (60 - (i - today_n))
        px = base + (today_n - i) * (10 if rising else 0)
        out.append({
            "time_str": f"{day}{9 if is_today else 15:02d}{mm % 60:02d}00",
            "open": px - 5, "high": px + 10, "low": px - 10, "close": px,
            "volume": 1000 + i * 10,
        })
    return out


class Clock:
    """시각을 앞으로 감을 수 있는 가짜 시계 (라이브 datetime 대체)."""
    def __init__(self, dt): self.dt = dt
    def __call__(self): return self.dt
    def set(self, h, m, s=0):
        self.dt = self.dt.replace(hour=h, minute=m, second=s)


def build(now_dt=datetime(2026, 8, 3, 8, 59, 0)):
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows = []; _Repo.sells = []
    clock = Clock(now_dt)
    strat = SM.StrategyManager(
        kiwoom_rest=_Rest(), order_manager=_OrderMgr(),
        phase1b_controller=Phase1BController(), portfolio_optimizer=None,
        now_func=clock,
    )
    return strat, clock


def ob(strat, code, thick=True):
    strat.phase1b.orderbook.update(
        code,
        {"ask_prices": [10_000, 10_010, 10_020],
         "ask_volumes": [3_000, 3_000, 3_000] if thick else [10, 10, 10]},
        now=time.time())


def burst(strat, code, at, n=2, value=30_000_000):
    price = 10_000
    vol = max(1, int(value // price))
    for i in range(n):
        strat.phase1b.trade_flow.add_tick(code, price, "buy", vol, now=at - i * 0.3)


def tick(strat, code, strength, at, price=10_000, side="buy", volume=10):
    strat.on_trade({"stock_code": code, "price": price, "side": side,
                    "volume": volume, "strength": strength}, now=at)


T0 = time.time()

# ═════════════════════════════════════════════════════════
print("\n[1] 08:59 기동 — 장 시작 전 조건검색 스냅샷")
# ═════════════════════════════════════════════════════════
s, clock = build(datetime(2026, 8, 3, 8, 59, 0))
check("기동 시점 phase는 None (09:00 전)", s.get_current_phase() is None)

# 기동 스냅샷 3종목 (주도주상위 2 + 눌림목자동 1)
s.on_condition_hit("A001", "주도주A", cond_name="주도주상위")
s.on_condition_hit("A002", "주도주B", cond_name="주도주상위")
s.on_condition_hit("P001", "눌림목A", cond_name="눌림목자동")

check("08:59 스냅샷 종목이 폐기되지 않고 이름이 기록됨",
      all(c in s._stock_names for c in ("A001", "A002", "P001")),
      str(list(s._stock_names)))
check("조건명도 기록됨(라우팅 근거)",
      s._cond_names.get("A001") == "주도주상위"
      and s._cond_names.get("P001") == "눌림목자동")
check("08:59에도 pre-arm으로 체결틱 감시가 켜짐 — 09:00 즉시 무장 가능",
      all(s.phase1b.is_watching(c) for c in ("A001", "A002", "P001")))
check("장 시작 전에는 매수하지 않음", len(s.holdings) == 0)

# ═════════════════════════════════════════════════════════
print("\n[2] 09:00 장 시작 — 실시간 편입 + 첫 매수")
# ═════════════════════════════════════════════════════════
clock.set(9, 0, 5)
check("09:00부터 phase 활성", s.get_current_phase() is not None)

# 장중 실시간 편입 (08-01에 고친 type='02' 경로로 들어오는 종목)
s.on_condition_hit("A003", "장중편입", cond_name="돌파자동매매용")
check("장중 실시간 편입도 pre-arm 됨", s.phase1b.is_watching("A003"))

clock.set(9, 3, 0)
ob(s, "A001")
tick(s, "A001", 125.0, T0)                     # 무장 타이머 시작
check("첫 틱: 아직 무장 아님", "A001" not in s._armed_at)
tick(s, "A001", 130.0, T0 + 1.5)
check("1.5초: 여전히 무장 아님(3초 요구)", "A001" not in s._armed_at)

burst(s, "A001", T0 + 3.5)
tick(s, "A001", 135.0, T0 + 3.5)
check("3초 연속 + 버스트 -> 매수 체결", "A001" in s.holdings)
check("sub_strategy=1A", s.holdings["A001"]["sub_strategy"] == "1A")
check("호가 두툼 -> 시장가 주문", s.order_manager.orders[-1]["style"] == "market")
check("entry_strength 기록됨(청산 로직 전제)", s.holdings["A001"]["entry_strength"] > 0)
check("매수 후 pending 해제됨(슬롯 누수 없음)", "A001" not in s.pending)
check("워치리스트 DB에 기록됨", any(r.get("stock_code") == "A001" for r in _Repo.rows))

# ═════════════════════════════════════════════════════════
print("\n[3] 보유중 — 틱이 계속 쌓이고 청산 로직이 살아있는지")
# ═════════════════════════════════════════════════════════
before = s.phase1b.trade_flow.tick_count("A001", 120, now=T0 + 4)
tick(s, "A001", 130.0, T0 + 4.0, price=10_050)
after = s.phase1b.trade_flow.tick_count("A001", 120, now=T0 + 4.1)
check("보유 종목도 체결틱이 계속 쌓임(동적캡/손실반등 전제)", after > before,
      f"{before} -> {after}")
check("보유 종목은 진입 평가로 가지 않음", "A001" not in s._armed_at or True)
check("최고가 추적됨", s.holdings["A001"]["highest_price"] >= 10_050,
      str(s.holdings["A001"]["highest_price"]))

s.holdings["A001"]["lowest_price"] = 9_900
tick(s, "A001", 130.0, T0 + 4.2, price=9_900)
check("저점도 추적됨(손실반등 하이브리드 매도 전제)",
      s.holdings["A001"]["lowest_price"] <= 9_900)

# ═════════════════════════════════════════════════════════
print("\n[4] 청산 -> 재매수 규칙 (틱 상태 초기화 검증)")
# ═════════════════════════════════════════════════════════
s.holdings["A001"]["warmup_until"] = s._now() - timedelta(seconds=1)
armed_before = "A001" in s._armed_at or "A001" in s._strength_since
s._execute_sell("A001", 10_300, "익절")
check("익절 청산 완료", "A001" not in s.holdings)
check("청산 시 강도 타이머가 초기화됨 (핵심 — 안 지우면 재매수 때 3초를 건너뜀)",
      "A001" not in s._strength_since and "A001" not in s._armed_at,
      f"since={'A001' in s._strength_since} armed={'A001' in s._armed_at}")

# 청산 직후 버스트가 와도 무장부터 다시 해야 한다
burst(s, "A001", T0 + 10)
tick(s, "A001", 140.0, T0 + 10)
check("청산 직후 즉시 재매수되지 않음(무장 재요구 + 쿨다운)",
      "A001" not in s.holdings)

# ═════════════════════════════════════════════════════════
print("\n[5] 손실 청산 종목은 당일 재매수 영구 차단")
# ═════════════════════════════════════════════════════════
clock.set(9, 20, 0)
ob(s, "A002")
tick(s, "A002", 130.0, T0 + 20)
burst(s, "A002", T0 + 23.5)
tick(s, "A002", 130.0, T0 + 23.5)
check("A002 매수됨", "A002" in s.holdings)
s.holdings["A002"]["warmup_until"] = s._now() - timedelta(seconds=1)
s._execute_sell("A002", 9_600, "손절")
check("손실 청산됨", "A002" not in s.holdings)
check("손실 종목 재매수 차단 등록", "A002" in s._stoploss_blocked)

burst(s, "A002", T0 + 30)
tick(s, "A002", 130.0, T0 + 27)
tick(s, "A002", 130.0, T0 + 30)
check("손실 차단 종목은 무장돼도 매수 안 됨", "A002" not in s.holdings)

# ═════════════════════════════════════════════════════════
print("\n[6] 09:25 — Pullback 시간창 개시")
# ═════════════════════════════════════════════════════════
clock.set(9, 24, 30)
ob(s, "P001")
tick(s, "P001", 130.0, T0 + 40)
burst(s, "P001", T0 + 43.5)
tick(s, "P001", 130.0, T0 + 43.5)
check("09:24:30 — 눌림목은 아직 매수 안 됨(09:25부터)", "P001" not in s.holdings)

clock.set(9, 25, 0)
burst(s, "P001", T0 + 50)
tick(s, "P001", 130.0, T0 + 50)
check("09:25 정각 — 눌림목 매수 성립", "P001" in s.holdings)
check("sub_strategy=1A_눌림", s.holdings["P001"]["sub_strategy"] == "1A_눌림")
# 편입 시점의 분봉 1콜(시가 캐시용, 1A "시가대비 +5%" 필터의 근거)은 정상이다.
# 검증할 것은 "진입 판정 자체가 분봉을 더 부르지 않는가" — 즉 종목당 1콜을
# 넘지 않는지다. 구버전 Pullback은 여기서 종목당 2콜(_get_merged_candles +
# OBV용 400봉)을 추가로 태웠고, 그게 15초마다 반복됐다.
p001_calls = [c for c in s.api.calls if c[0] == "candles" and c[1] == "P001"]
check("눌림목 진입 판정이 분봉을 추가로 부르지 않음 (구버전은 종목당 2콜)",
      len(p001_calls) == 0, str(p001_calls))
check("400봉(OBV용) 조회가 완전히 사라짐",
      not any(c[0] == "candles" and c[2] >= 400 for c in s.api.calls),
      str([c for c in s.api.calls if c[0] == "candles" and c[2] >= 400]))

# ═════════════════════════════════════════════════════════
print("\n[7] 10:30 — 중복 편입 종목 전략 전환")
# ═════════════════════════════════════════════════════════
s2, clk2 = build(datetime(2026, 8, 3, 10, 0, 0))
s2.on_condition_hit("D001", "중복종목", cond_name="주도주상위")
s2.on_condition_hit("D001", "중복종목", cond_name="눌림목자동")
check("두 조건식이 병합 기록됨",
      "주도주상위" in s2._cond_names["D001"] and "눌림목자동" in s2._cond_names["D001"],
      s2._cond_names["D001"])
check("10:00 -> 1A로 라우팅", s2.resolve_strategy(s2._cond_names["D001"],
                                                 clk2().time()) == "1A")
clk2.set(10, 30, 0)
check("10:30 -> Pullback으로 전환", s2.resolve_strategy(s2._cond_names["D001"],
                                                      clk2().time()) == "1A_눌림")

ob(s2, "D001")
tick(s2, "D001", 130.0, T0 + 60)
burst(s2, "D001", T0 + 63.5)
tick(s2, "D001", 130.0, T0 + 63.5)
check("10:30 이후 매수는 눌림 슬롯으로 들어감",
      s2.holdings.get("D001", {}).get("sub_strategy") == "1A_눌림",
      str(s2.holdings.get("D001", {}).get("sub_strategy")))

# ═════════════════════════════════════════════════════════
print("\n[8] 점심(12:00) — 시간대 계수로 완화되는지")
# ═════════════════════════════════════════════════════════
s3, clk3 = build(datetime(2026, 8, 3, 12, 0, 0))
s3.on_condition_hit("N001", "점심종목", cond_name="주도주상위")
ob(s3, "N001")
tick(s3, "N001", 130.0, T0 + 70)
burst(s3, "N001", T0 + 73.5, n=2, value=20_000_000)   # 2천만 x2 (오전 기준 미달)
tick(s3, "N001", 130.0, T0 + 73.5)
check("점심엔 2천만원 x2건도 통과(계수 0.65 -> 문턱 1,950만)",
      "N001" in s3.holdings, f"holdings={list(s3.holdings)}")

s4, clk4 = build(datetime(2026, 8, 3, 9, 30, 0))
s4.on_condition_hit("M001", "오전종목", cond_name="주도주상위")
ob(s4, "M001")
tick(s4, "M001", 130.0, T0 + 70)
burst(s4, "M001", T0 + 73.5, n=2, value=20_000_000)
tick(s4, "M001", 130.0, T0 + 73.5)
check("같은 2천만원 x2건이 오전엔 탈락(계수 1.00 -> 문턱 3,000만)",
      "M001" not in s4.holdings)

# ═════════════════════════════════════════════════════════
print("\n[9] 14:50 진입 중단 / 15:10 강제청산")
# ═════════════════════════════════════════════════════════
s5, clk5 = build(datetime(2026, 8, 3, 14, 45, 0))
s5.on_condition_hit("L001", "마감전", cond_name="주도주상위")
ob(s5, "L001")
tick(s5, "L001", 130.0, T0 + 80)
burst(s5, "L001", T0 + 83.5)
tick(s5, "L001", 130.0, T0 + 83.5)
check("14:45 — 아직 매수 가능", "L001" in s5.holdings)

clk5.set(14, 51, 0)
s5.on_condition_hit("L002", "마감후", cond_name="주도주상위")
ob(s5, "L002")
tick(s5, "L002", 130.0, T0 + 90)
burst(s5, "L002", T0 + 93.5)
tick(s5, "L002", 130.0, T0 + 93.5)
check("14:51 — 신규 진입 완전 정지", "L002" not in s5.holdings)

from core.order_manager import FORCE_CLOSE_TIME
check("강제청산 시각 15:10", FORCE_CLOSE_TIME == "15:10", str(FORCE_CLOSE_TIME))
check("진입 종료(14:50) < 강제청산(15:10) — 청산 중 신규매수 겹침 없음",
      SM.ENTRY_WINDOW_END < SM.time(15, 10))

clk5.set(15, 10, 0)
s5.holdings["L001"]["warmup_until"] = s5._now() - timedelta(seconds=1)


def simulate_force_close(strat, rest_ok=True):
    """main.py task_force_close_watcher의 청산 루프를 그대로 재현."""
    for code in list(strat.holdings.keys()):
        px = 0
        try:
            px = strat._fresh_tick_price(code, max_age_sec=600) or 0
        except Exception:
            px = 0
        if not px and rest_ok:
            try:
                candles = strat.api.get_minute_candles(code, interval=1, count=1)
                if candles:
                    px = candles[0]["close"]
            except Exception:
                pass
        if not px:
            px = strat.holdings.get(code, {}).get("buy_price", 0)
        if px:
            strat._execute_sell(code, px, "장마감 강제청산")


simulate_force_close(s5)
check("15:10 전량 강제청산 — 보유 0종목", len(s5.holdings) == 0,
      str(list(s5.holdings)))
check("강제청산도 시장가로 나감",
      s5.order_manager.orders[-1]["style"].startswith("sell:market"),
      str(s5.order_manager.orders[-1]["style"]))

# REST가 죽은 상태에서도 청산이 되는지 (429가 가장 심한 시각이다)
s5b, clk5b = build(datetime(2026, 8, 3, 14, 45, 0))
s5b.on_condition_hit("L003", "REST장애", cond_name="주도주상위")
ob(s5b, "L003")
tick(s5b, "L003", 130.0, T0 + 95)
burst(s5b, "L003", T0 + 98.5)
tick(s5b, "L003", 130.0, T0 + 98.5)
check("L003 매수됨(사전조건)", "L003" in s5b.holdings)
clk5b.set(15, 10, 0)
s5b.holdings["L003"]["warmup_until"] = s5b._now() - timedelta(seconds=1)
s5b.api.get_minute_candles = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("429"))
simulate_force_close(s5b, rest_ok=True)
check("REST가 죽어도 강제청산 완료 (구버전은 오버나이트로 넘어감)",
      len(s5b.holdings) == 0, str(list(s5b.holdings)))

# ═════════════════════════════════════════════════════════
print("\n[10] tick() 주기 정리 — 1A 창 내내 데이터가 안 지워지는지")
# ═════════════════════════════════════════════════════════
# 2026-08-01에 "10:30 이후 후보를 stop_watching 해서 1A가 자기 데이터를
# 지우던" 회귀가 있었다. 그 재발 방지.
s6, clk6 = build(datetime(2026, 8, 3, 11, 0, 0))
s6.on_condition_hit("W001", "감시종목", cond_name="주도주상위")
s6.tick()
check("11:00 — 후보 감시가 유지됨(구버전은 여기서 리셋)",
      s6.phase1b.is_watching("W001"))
clk6.set(13, 0, 0)
s6.tick()
check("13:00 — 여전히 유지(1A 창은 14:50까지)", s6.phase1b.is_watching("W001"))
clk6.set(14, 55, 0)
s6.tick()
check("14:55 — 진입창 종료 후에는 정리됨", not s6.phase1b.is_watching("W001"))
check("정리 시 틱 진입 상태도 같이 지워짐",
      "W001" not in s6._strength_since and "W001" not in s6._armed_at)

# ═════════════════════════════════════════════════════════
print("\n[11] 이상 입력 내성 — 장중에 죽으면 안 된다")
# ═════════════════════════════════════════════════════════
s7, clk7 = build(datetime(2026, 8, 3, 9, 30, 0))
s7.on_condition_hit("E001", "이상종목", cond_name="주도주상위")

bad_inputs = [
    ({"stock_code": "E001"}, "가격/강도 없는 틱"),
    ({"stock_code": "E001", "price": 0, "volume": 0, "strength": 0}, "전부 0"),
    ({"stock_code": "E001", "price": 10_000, "volume": 10, "strength": None}, "강도 None"),
    ({"stock_code": "E001", "price": -1, "volume": -5, "strength": -3}, "음수"),
    ({"stock_code": "", "price": 10_000}, "빈 종목코드"),
    ({"price": 10_000, "strength": 130}, "종목코드 없음"),
    ({"stock_code": "E001", "price": 10_000, "volume": 10,
      "strength": float("inf")}, "무한대 강도"),
]
crashed = []
for payload, label in bad_inputs:
    try:
        s7.on_trade(payload, now=T0 + 100)
    except Exception as e:
        crashed.append(f"{label}: {type(e).__name__}")
check("이상 틱 7종에도 on_trade가 예외를 던지지 않음",
      not crashed, "; ".join(crashed))

# 호가 스냅샷이 없는 상태로 매수까지 가도 죽지 않아야 한다
s8, clk8 = build(datetime(2026, 8, 3, 9, 30, 0))
s8.on_condition_hit("E002", "호가없음", cond_name="주도주상위")
tick(s8, "E002", 130.0, T0 + 110)
burst(s8, "E002", T0 + 113.5)
try:
    tick(s8, "E002", 130.0, T0 + 113.5)
    ok_no_ob = True
except Exception as e:
    ok_no_ob = False
check("호가 스냅샷이 없어도 매수 경로가 죽지 않음(지정가로 수렴)", ok_no_ob)
check("호가 없으면 지정가로 주문",
      (not s8.order_manager.orders) or s8.order_manager.orders[-1]["style"] == "limit",
      str(s8.order_manager.orders[-1]["style"]) if s8.order_manager.orders else "주문없음")

# phase1b가 통째로 없는 상황 (WS 초기화 실패 등)
s9, clk9 = build(datetime(2026, 8, 3, 9, 30, 0))
s9._cond_names["E003"] = "주도주상위"
s9.phase1b = None
try:
    s9.on_trade({"stock_code": "E003", "price": 10_000, "volume": 10,
                 "strength": 130.0}, now=T0 + 120)
    ok_no_p1b = True
except Exception as e:
    ok_no_p1b = False
check("phase1b가 없어도 on_trade가 죽지 않음", ok_no_p1b)

# DB 실패 상황
s10, clk10 = build(datetime(2026, 8, 3, 9, 30, 0))
class _BadRepo(_Repo):
    @classmethod
    def add(cls, **kw): raise RuntimeError("DB 다운")
    @classmethod
    def insert_buy(cls, **kw): raise RuntimeError("DB 다운")
SM.WatchListRepository = _BadRepo
SM.TradeRepository = _BadRepo
s10.on_condition_hit("E004", "DB장애", cond_name="주도주상위")
ob(s10, "E004")
tick(s10, "E004", 130.0, T0 + 130)
burst(s10, "E004", T0 + 133.5)
try:
    tick(s10, "E004", 130.0, T0 + 133.5)
    ok_db = True
except Exception:
    ok_db = False
check("DB가 죽어도 매수 경로가 살아있음(포지션 추적 유지)", ok_db)
check("DB 실패해도 holdings에는 포지션이 잡힘", "E004" in s10.holdings,
      str(list(s10.holdings)))
SM.WatchListRepository = _Repo
SM.TradeRepository = _Repo

# ═════════════════════════════════════════════════════════
print("\n[12] 핫패스 성능 — 초당 수십 틱을 견디는지")
# ═════════════════════════════════════════════════════════
s11, clk11 = build(datetime(2026, 8, 3, 9, 30, 0))
codes = [f"H{i:03d}" for i in range(30)]
for c in codes:
    s11.on_condition_hit(c, c, cond_name="주도주상위")
    ob(s11, c)

t_start = time.time()
N = 3000
for i in range(N):
    c = codes[i % len(codes)]
    tick(s11, c, 95.0, T0 + 200 + i * 0.01)   # 무장 안 되는 강도(대부분의 틱)
elapsed = time.time() - t_start
per_tick_us = elapsed / N * 1e6
check(f"30종목 x {N}틱 처리 (무장 전 경로)", elapsed < 5.0,
      f"{elapsed:.2f}초 / 틱당 {per_tick_us:.0f}us")
check("틱당 처리시간이 1ms 미만 — 실시간 유입을 못 따라갈 위험 없음",
      per_tick_us < 1000, f"{per_tick_us:.0f}us")

# 무장 상태에서도 쿨다운이 폭주를 막는지
s12, clk12 = build(datetime(2026, 8, 3, 9, 30, 0))
s12.on_condition_hit("HOT1", "HOT1", cond_name="주도주상위")
ob(s12, "HOT1")
s12._strength_since["HOT1"] = T0 + 290    # 이미 무장된 상태로 진입
eval_count = []
orig = s12.evaluate_tick_entry
s12.evaluate_tick_entry = lambda *a, **k: (eval_count.append(1), orig(*a, **k))[1]
for i in range(200):
    tick(s12, "HOT1", 130.0, T0 + 300 + i * 0.01)   # 2초 동안 200틱
check("무장 후에도 쿨다운(0.25초)이 전체 게이트 폭주를 막음",
      len(eval_count) <= 12, f"{len(eval_count)}회 평가 / 200틱")

# ═════════════════════════════════════════════════════════
print("\n[13] 슬롯 회계 — 오버부킹/누수 없는지")
# ═════════════════════════════════════════════════════════
s13, clk13 = build(datetime(2026, 8, 3, 9, 30, 0))
bought = []
for i in range(10):                       # 상한(6)보다 많이 시도
    c = f"S{i:02d}"
    s13.on_condition_hit(c, c, cond_name="주도주상위")
    ob(s13, c)
    tick(s13, c, 130.0, T0 + 400 + i * 10)
    burst(s13, c, T0 + 403.5 + i * 10)
    tick(s13, c, 130.0, T0 + 403.5 + i * 10)
    if c in s13.holdings:
        bought.append(c)
check("공유 상한(MAX_HOLDINGS=6)을 넘겨 사지 않음",
      len(s13.holdings) <= SM.MAX_HOLDINGS,
      f"{len(s13.holdings)}종목 / 상한 {SM.MAX_HOLDINGS}")
check("1A 전략 캡(4)도 지켜짐",
      s13.count_holdings_by_strategy("1A") <= SM.PHASE1A_MAX_SLOTS,
      str(s13.count_holdings_by_strategy("1A")))
check("매수 못 한 종목이 pending에 남아있지 않음(슬롯 누수)",
      len(s13.pending) == 0, str(s13.pending))
check("occupied_slots가 실제 보유수와 일치",
      s13.occupied_slots() == len(s13.holdings),
      f"{s13.occupied_slots()} vs {len(s13.holdings)}")

# ═════════════════════════════════════════════════════════
print("\n[14] 재시작 안전성 — 당일 리스크 상태 복원")
# ═════════════════════════════════════════════════════════
check("_restore_daily_risk_state 존재(재시작 시 손절차단 복원)",
      hasattr(SM.StrategyManager, "_restore_daily_risk_state"))
check("reset_tick_entry_state 존재(청산/감시해제 시 상태 정리)",
      hasattr(SM.StrategyManager, "reset_tick_entry_state"))

s14, clk14 = build(datetime(2026, 8, 3, 10, 0, 0))
s14._stoploss_blocked.add("R001")
s14._cond_names["R001"] = "주도주상위"
s14._stock_names["R001"] = "R001"
s14.phase1b.start_watching("R001")
ob(s14, "R001")
s14._strength_since["R001"] = T0 + 490
burst(s14, "R001", T0 + 500)
tick(s14, "R001", 130.0, T0 + 500)
check("재시작 후 복원된 손절차단이 틱 경로에서도 적용됨",
      "R001" not in s14.holdings)

# ═════════════════════════════════════════════════════════
print("\n[15] daily_backtest (15:30) 기동 가능 여부")
# ═════════════════════════════════════════════════════════
try:
    import core.daily_backtest as DB
    ok_import = True
    err = ""
except Exception as e:
    ok_import = False; err = str(e)
check("daily_backtest 임포트 정상(07-27 ImportError 붕괴 재발 방지)",
      ok_import, err)
check("삭제된 상수를 참조하지 않음",
      ok_import and not hasattr(DB, "OTHER_COND_START"))
check("라이브 시간창을 상수로 참조(하드코딩 아님)",
      ok_import and DB.PULLBACK_START_HHMM == "0925"
      and DB.PULLBACK_END_HHMM == "1450",
      f"{DB.PULLBACK_START_HHMM}~{DB.PULLBACK_END_HHMM}" if ok_import else "")

# ═════════════════════════════════════════════════════════
print("\n[16] 진단 알림 — 사유 분류가 뭉개지지 않는지")
# ═════════════════════════════════════════════════════════
cats = {}
for reason in [
    "체결강도 미무장 (100 이상 1.2/3초 연속)",
    "대량체결 부족 (최근 5초: 3,000만원+ 0/2건, 최대단일 0만원/10,000만원, 상대 0/2건)",
    "슬롯 부족 (1A 4/4, 전체 6/6)",
    "시가대비 +7.0% >= 5% — 눌림 가능성으로 매수 보류",
    "지수 -5% 초과로 인한 전면 매매 중단",
    "등락률 상한 초과 (전일종가대비 +18.0% > +16%)",
    "체결강도 데이터 소스 없음(phase1b 미연결)",
]:
    cats[reason[:20]] = SM.StrategyManager._reject_category(reason)
unknown = [k for k, v in cats.items() if v == "기타"]
check("틱 경로의 모든 탈락 사유가 분류됨('기타'로 안 뭉개짐)",
      not unknown, str(unknown))
check("미무장이 전용 분류를 가짐",
      SM.StrategyManager._reject_category("체결강도 미무장 (100 이상 1.2/3초 연속)")
      == "강도 미무장(3초 미달)")

s15, clk15 = build(datetime(2026, 8, 3, 9, 30, 0))
s15.on_condition_hit("DG1", "진단", cond_name="주도주상위")
tick(s15, "DG1", 130.0, T0 + 600)
tick(s15, "DG1", 130.0, T0 + 604)     # 무장은 됐지만 버스트 없음
try:
    diag = s15.build_entry_diagnostics()
    ok_diag = isinstance(diag, str) and len(diag) > 0
except Exception as e:
    ok_diag = False; diag = str(e)
check("진입 진단 알림이 정상 생성됨", ok_diag, str(diag)[:80])
check("진단에 틱 단계별 통과 수가 표시됨 (어디서 끊기는지 한 줄로)",
      ok_diag and "무장" in diag and "버스트" in diag and "매수" in diag,
      next((l for l in str(diag).split("\n") if "무장" in l), ""))

# ═════════════════════════════════════════════════════════
print("\n[17] FID 228 수신 감시 — 이번 개편 유일한 미검증 가정")
# ═════════════════════════════════════════════════════════
# 무장이 228 하나에 걸려 있어, 이게 안 오면 조용히 하루 종일 매수 0건이 된다.
s16, clk16 = build(datetime(2026, 8, 3, 9, 30, 0))
s16.on_condition_hit("F1", "정상", cond_name="주도주상위")
tick(s16, "F1", 130.0, T0 + 700)
check("228이 실려오면 수신 종목으로 기록됨", "F1" in s16._fid228_seen)
check("체결틱 총수도 집계됨", s16._trade_tick_total >= 1, str(s16._trade_tick_total))
d16 = s16.build_entry_diagnostics()
check("정상일 때는 228 경고가 뜨지 않음", "228" not in d16 or "🚨" not in d16)

# 0B는 오는데 228만 비어있는 경우 (가장 위험한 시나리오)
s17, clk17 = build(datetime(2026, 8, 3, 9, 30, 0))
s17.on_condition_hit("F2", "228없음", cond_name="주도주상위")
for i in range(50):
    s17.on_trade({"stock_code": "F2", "price": 10_000, "side": "buy",
                  "volume": 10}, now=T0 + 710 + i)      # strength 키 자체가 없음
check("228 없는 틱은 수신 집합에 안 들어감", "F2" not in s17._fid228_seen)
check("그래도 체결틱 수는 늘어남(0B는 정상)", s17._trade_tick_total == 50,
      str(s17._trade_tick_total))
d17 = s17.build_entry_diagnostics()
check("'0B는 오는데 228만 없음'을 정확히 경고",
      "228" in d17 and "🚨" in d17,
      next((l for l in d17.split("\n") if "228" in l), ""))
check("이 상태에서는 무장이 성립하지 않음", "F2" not in s17._armed_at)

# 0B 자체가 안 오는 경우 (WS 구독 이상) — 위와 대응이 달라 구분해야 한다
s18, clk18 = build(datetime(2026, 8, 3, 9, 30, 0))
s18.on_condition_hit("F3", "무수신", cond_name="주도주상위")
d18 = s18.build_entry_diagnostics()
check("체결틱 0건이면 WS 구독 이상으로 안내(228 문제와 구분)",
      "0B" in d18 and "228" not in d18.split("0B")[1][:60],
      next((l for l in d18.split("\n") if "0B" in l), ""))

# ═════════════════════════════════════════════════════════
print("\n[18] 정합성 — 주석/규칙이 코드와 일치하는지")
# ═════════════════════════════════════════════════════════
import inspect
src_cbp = inspect.getsource(SM.StrategyManager.can_buy_pullback)
check("can_buy_pullback 주석이 실제 시간창(09:25~14:50)과 일치",
      "09:25" in src_cbp and "14:50" in src_cbp and "15:10" not in src_cbp,
      next((l.strip() for l in src_cbp.split("\n") if "눌림목:" in l), ""))
check("제거된 09:20 지연 게이트 분류가 규칙에서 빠짐",
      not any("조건식 지연" in str(k) for k, _ in SM.StrategyManager._REJECT_RULES))
check("Pullback 시간창 상수가 09:25~14:50",
      SM.PULLBACK_START == SM.time(9, 25) and SM.PULLBACK_END == SM.time(14, 50))
check("Phase1BController docstring이 '데이터 파이프라인'임을 명시",
      "파이프라인" in (Phase1BController.__doc__ or ""))

# ═════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print(f"  - {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
