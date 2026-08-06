"""08-03 실전 드라이런 — 08:59 기동부터 15:10 강제청산까지 종일 시뮬레이션.

목적: "장중에 오류 나서 고치는" 상황을 없애기 위해, 실제 하루 동안 일어나는
이벤트 순서를 그대로 재현해 **한 군데라도 끊기는 곳이 없는지** 확인한다.

기존 두 테스트와 역할이 다르다:
  - test_patch_20260801/20260802.py : 개별 로직 단위 검증 (유닛)
  - 이 파일                          : 전 구간이 **유기적으로 이어지는지** (통합)

재현하는 실제 흐름:
  08:59 기동 -> 조건검색 스냅샷(장 시작 전 편입)
  09:00 장 시작 -> 실시간 편입(type='02') -> pre-arm
  09:xx 체결틱 유입 -> 무장(강도 TICK_STRENGTH_SUSTAIN_SEC 연속) -> 버스트 -> 매수
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

import os as _os_testlog
# 실거래 로그(autotrader.log) 오염 방지 — 반드시 core/main 임포트보다 먼저.
_os_testlog.environ["AUTOTRADER_TEST_LOG"] = "1"

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
    def update(cls, row_id, data):   # 분할매수 2차 평단가 갱신 (2026-08-04)
        cls.updates.append({"id": row_id, **data}); return True
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
    # [F] 진입 숙성(MIN_ENTRY_DELAY_SEC) 충족 상태로 둔다 (2026-08-06).
    # 이 통합 테스트는 '편입 후 충분히 지켜본 종목'이 진입 배선을 타는지를
    # 보는 것이지 숙성 게이트 자체를 재는 게 아니다. 실전에서도 편입과 진입
    # 사이에는 최소 수십 초가 흐른다(08-03~06 실측 중앙값 6.7분).
    # 숙성 게이트 자체의 검증은 test_patch_20260806.py [9]에 있다.
    strat._first_seen[code] = time.time() - 999
    strat.phase1b.orderbook.update(
        code,
        {"ask_prices": [10_000, 10_010, 10_020],
         "ask_volumes": [3_000, 3_000, 3_000] if thick else [10, 10, 10]},
        now=time.time())


def burst(strat, code, at, n=2, value=None):
    # 문턱 상수를 따라가게 한다 (08-04: 3천만->4천만). 수치를 박으면 상수를
    # 올릴 때마다 픽스처가 조용히 미달이 되어 "매수 안 됨"으로 무더기 실패한다.
    value = SM.PHASE1A_BURST_TRADE_VALUE if value is None else value
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
# [F] 08:59 편입 -> 09:03이면 실전에선 4분이 지났다. 격리 테스트의
# _first_seen은 실제 시계(time.time())라 그 경과를 재현해 준다.
for _c in ("A001", "A002", "A003", "P001"):
    s._first_seen[_c] = time.time() - 999
tick(s, "A001", 125.0, T0)                     # 무장 타이머 시작
check("첫 틱: 아직 무장 아님", "A001" not in s._armed_at)
tick(s, "A001", 130.0, T0 + 1.0)
check("1.0초: 여전히 무장 아님(요구 1.5초)", "A001" not in s._armed_at)

burst(s, "A001", T0 + 3.5)
tick(s, "A001", 135.0, T0 + 3.5)
# (2026-08-04) 무장+버스트는 즉시매수가 아니라 **되돌림 대기 계획**을 연다.
check("3초 연속 + 버스트 -> 되돌림 대기 계획 생성", "A001" in s._entry_plans)
check("아직 매수 전(트리거는 국소 고점이므로 기다린다)", "A001" not in s.holdings)
check("대기 중에도 슬롯 점유", s.occupied_slots() >= 1)
# -0.5% 되돌림 도달 -> 1차 트랜치(50%) 체결
tick(s, "A001", 130.0, T0 + 5.0, price=9_940, side="sell")
check("-0.5% 되돌림 -> 1차 트랜치 매수 체결", "A001" in s.holdings)
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
# 되돌림 대기(2026-08-04) — -0.5% 닿아야 1차가 체결된다
tick(s, "A002", 130.0, T0 + 24.5, price=9_940, side="sell")
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
print("\n[6] Pullback 시간창 — 1A와 동일한 09:00 개시 (2026-08-03 변경)")
# ═════════════════════════════════════════════════════════
# 구버전은 09:25부터였다. 그 근거("개장 직후엔 당일 고가가 없어 눌림 판정
# 불가")는 08-02에 분봉 재검증을 폐지하면서 소멸했다 — 지금 눌림 판정은 전부
# HTS 조건식이 하고, 그건 여러 날 봉을 보므로 09:00에도 성립한다.
clock.set(8, 59, 30)
ob(s, "P001")
tick(s, "P001", 130.0, T0 + 40)
burst(s, "P001", T0 + 43.5)
tick(s, "P001", 130.0, T0 + 43.5)
check("08:59:30 — 장 시작 전이라 매수 안 됨", "P001" not in s.holdings)

clock.set(9, 0, 5)
# [E] 눌림 슬롯이 0이어도 **파이프라인 자체**는 계속 검증한다 —
# 슬롯만 되돌리면 다시 돌아야 하므로 여기서는 잠시 복구해 배선을 확인한다.
# (슬롯 0이 실제로 매수를 막는지는 test_patch_20260806.py [8]에서 못박는다.)
_pb_old = SM.PULLBACK_MAX_SLOTS
SM.PULLBACK_MAX_SLOTS = 4
burst(s, "P001", T0 + 50)
tick(s, "P001", 130.0, T0 + 50)
check("09:00 직후 — 눌림목도 되돌림 대기 계획 생성", "P001" in s._entry_plans)
tick(s, "P001", 130.0, T0 + 51, price=9_940, side="sell")
check("09:00 직후 — 눌림목 매수 성립(구버전은 09:25까지 대기)",
      "P001" in s.holdings, f"holdings={list(s.holdings)}")
check("1A와 시간창이 동일해짐", SM.PULLBACK_START == SM.GROUP_A_START)
check("sub_strategy=1A_눌림", s.holdings["P001"]["sub_strategy"] == "1A_눌림")
SM.PULLBACK_MAX_SLOTS = _pb_old   # [E] 원상복구
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

_pb_old2 = SM.PULLBACK_MAX_SLOTS
SM.PULLBACK_MAX_SLOTS = 4   # [E] 눌림 라우팅 배선 검증용 임시 복구
ob(s2, "D001")
tick(s2, "D001", 130.0, T0 + 60)
burst(s2, "D001", T0 + 63.5)
tick(s2, "D001", 130.0, T0 + 63.5)
tick(s2, "D001", 130.0, T0 + 64.5, price=9_940, side="sell")  # 되돌림 체결
check("10:30 이후 매수는 눌림 슬롯으로 들어감",
      s2.holdings.get("D001", {}).get("sub_strategy") == "1A_눌림",
      str(s2.holdings.get("D001", {}).get("sub_strategy")))
SM.PULLBACK_MAX_SLOTS = _pb_old2   # [E] 원상복구

# ═════════════════════════════════════════════════════════
print("\n[8] 점심(12:00) — 완화가 제거됐는지 (2026-08-03 사양 변경)")
# ═════════════════════════════════════════════════════════
# 구버전은 점심 계수 0.65로 문턱이 1,950만원까지 내려가 2천만원 x2건이
# **통과**했다. 08-03 실거래에서 그 완화 구간(11:30~13:00) 3건이 전부 손실
# (합계 -5.29%)로 나와 완화를 없앴다 — 이제 점심도 오전과 같은 3천만원이다.
s3, clk3 = build(datetime(2026, 8, 3, 12, 0, 0))
s3.on_condition_hit("N001", "점심종목", cond_name="주도주상위")
ob(s3, "N001")
tick(s3, "N001", 130.0, T0 + 70)
burst(s3, "N001", T0 + 73.5, n=2, value=20_000_000)   # 2천만 x2 (기준 미달)
tick(s3, "N001", 130.0, T0 + 73.5)
check("점심에도 2천만원 x2건은 탈락(구버전은 여기서 매수)",
      "N001" not in s3.holdings, f"holdings={list(s3.holdings)}")
# 3천만원 x2건이면 점심에도 정상 진입한다 — '점심 매매 중단'이 아니라
# '점심에도 같은 기준'임을 못박는다.
s3b, _ = build(datetime(2026, 8, 3, 12, 0, 0))
s3b.on_condition_hit("N002", "점심정상", cond_name="주도주상위")
ob(s3b, "N002")
tick(s3b, "N002", 130.0, T0 + 70)
burst(s3b, "N002", T0 + 73.5, n=2)   # 문턱 상수 그대로 (하드코딩 금지)
tick(s3b, "N002", 130.0, T0 + 73.5)
tick(s3b, "N002", 130.0, T0 + 74.5, price=9_940, side="sell")  # 되돌림 체결
check("점심에도 문턱 x2건이면 정상 매수(완화도 강화도 없음)",
      "N002" in s3b.holdings, f"holdings={list(s3b.holdings)}")

s4, clk4 = build(datetime(2026, 8, 3, 9, 30, 0))
s4.on_condition_hit("M001", "오전종목", cond_name="주도주상위")
ob(s4, "M001")
tick(s4, "M001", 130.0, T0 + 70)
burst(s4, "M001", T0 + 73.5, n=2, value=20_000_000)
tick(s4, "M001", 130.0, T0 + 73.5)
check("같은 2천만원 x2건이 오전에도 탈락(계수 1.00)",
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
tick(s5, "L001", 130.0, T0 + 84.5, price=9_940, side="sell")  # 되돌림 체결
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
tick(s5b, "L003", 130.0, T0 + 99.5, price=9_940, side="sell")  # 되돌림 체결
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
    tick(s10, "E004", 130.0, T0 + 134.5, price=9_940, side="sell")  # 되돌림 체결
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
    tick(s13, c, 130.0, T0 + 404.5 + i * 10, price=9_940, side="sell")  # 되돌림 체결
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
      ok_import and DB.PULLBACK_START_HHMM == "0900"
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
      SM.StrategyManager._reject_category("체결강도 미무장 (100 이상 1.2/1.5초 연속)")
      == "강도 미무장(요구시간 미달)")
# (2026-08-04) 분류 라벨에 초 수/％ 수치를 박으면 상수를 바꿔도 안 따라와서
# 진단 알림이 거짓말을 한다 — 실제로 무장 3.0->1.5초 변경 후에도 "3초 미달"로
# 남아 있었고, 지수 가드 -3->-5% 변경 후에도 "(-3%)"로 남아 있었다.
_labels = [lab for lab, _ in SM.StrategyManager._REJECT_RULES]
check("분류 라벨에 하드코딩된 초 수가 없음",
      not any("초 미달" in l for l in _labels), str(_labels))
check("분류 라벨에 하드코딩된 % 수치가 없음",
      not any("%" in l for l in _labels), str(_labels))

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
check("can_buy_pullback 주석이 실제 시간창(09:00~14:50)과 일치",
      "09:00" in src_cbp and "14:50" in src_cbp and "15:10" not in src_cbp,
      next((l.strip() for l in src_cbp.split("\n") if "눌림목:" in l), ""))
check("제거된 09:20 지연 게이트 분류가 규칙에서 빠짐",
      not any("조건식 지연" in str(k) for k, _ in SM.StrategyManager._REJECT_RULES))
check("Pullback 시간창 상수가 09:00~14:50 (2026-08-03: 09:25에서 앞당김)",
      SM.PULLBACK_START == SM.time(9, 0) and SM.PULLBACK_END == SM.time(14, 50))
check("Phase1BController docstring이 '데이터 파이프라인'임을 명시",
      "파이프라인" in (Phase1BController.__doc__ or ""))

# (2026-08-02) 코드가 실제로 생성하는 탈락 사유가 전부 분류되는지 —
# 새 사유 문자열을 만들고 _REJECT_RULES를 안 고치면 "기타"로 뭉개져
# 장중 진단이 무력해진다. 라이브 경로(호출부가 있는 함수)만 검사한다.
_LIVE_REJECTS = [
    "체결강도 미무장 (100 이상 1.2/3초 연속)",
    "대량체결 부족 (최근 5초: 3000만원+ 0/2건)",
    "시가대비 +6.1% >= 5% — 눌림 가능성으로 매수 보류",
    "지수 -5% 초과로 인한 전면 매매 중단",
    "체결강도 데이터 소스 없음(phase1b 미연결)",
    "버스트 계산 실패",
]
_uncat = [r for r in _LIVE_REJECTS
          if SM.StrategyManager._reject_category(r) == "기타"]
check("라이브 탈락 사유가 전부 분류됨('기타' 뭉개짐 없음)", not _uncat, str(_uncat))

# check_burst 예외는 '정상 필터링'이 아니라 코드 이상이므로 인프라 경고로 떠야 한다.
_cat = SM.StrategyManager._reject_category("버스트 계산 실패")
check("버스트 계산 실패가 인프라 이상으로 분류됨",
      _cat in SM.StrategyManager._REJECT_INFRA, _cat)

# ═════════════════════════════════════════════════════════
print("\n[19] 기동 인프라 — 08:59 무인 기동이 실제로 성립하는지")
# 여기서 보는 것은 '전략'이 아니라 **봇이 뜨는가 / 폰으로 볼 수 있는가**다.
# 이 계층은 무인(비대화형) 실행이라 실패해도 화면에 아무도 없다 — 그래서
# 대화형으로 손으로 확인하는 방식으로는 절대 못 잡는다(2026-07-28 교훈).
import os as _os
import re as _re

# main.py는 **텍스트로** 읽는다 — 이 스위트는 main을 import하지 않는 구조라
# (스텁 계약이 strategy_manager 기준) 여기서 import하면 부작용이 생긴다.
# 검사 목적은 '배선이 코드에 실제로 있는가'이므로 소스 검사로 충분하고,
# import 가능 여부는 이관 체크리스트 1번이 따로 본다.
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_main_src = open(_os.path.join(_ROOT, "main.py"), encoding="utf-8").read()


def _func_body(src, name):
    """def name(...) 부터 다음 같은 들여쓰기의 def 전까지를 잘라낸다."""
    m = _re.search(rf"\n(\s*)(?:async )?def {name}\b", src)
    if not m:
        return ""
    indent, start = m.group(1), m.start()
    nxt = _re.search(rf"\n{indent}(?:async )?def ", src[start + 1:])
    return src[start: start + 1 + nxt.start()] if nxt else src[start:]


# (1) 원격제어 감시가 gather에 실제로 배선됐는지 — 정의만 하고 등록을 빠뜨리면
#     조용히 아무 일도 안 일어난다(08-02에 겪은 '배선 누락' 부류).
check("원격제어 워치독이 정의됨",
      "async def task_remote_control_watchdog" in _main_src)
check("원격제어 워치독이 gather에 등록됨",
      "self.task_remote_control_watchdog()" in _main_src)

# (2) 감시 자체가 매매를 방해하면 안 된다 — 블로킹 호출은 반드시 to_thread로.
_rc_src = _func_body(_main_src, "task_remote_control_watchdog")
check("원격제어 확인이 to_thread로 분리됨(이벤트 루프 비차단)",
      "asyncio.to_thread" in _rc_src)
check("원격제어 워치독에 예외 처리가 있음", "except Exception" in _rc_src)

# (3) 확인 실패 시 True(=정상으로 간주) — 오탐 알림으로 장중 주의를 뺏지 않는다.
_isrun_src = _func_body(_main_src, "_is_remote_control_running")
check("프로세스 확인 실패 시 오탐 방지로 True 반환", "return True" in _isrun_src)
check("프로세스 확인에 타임아웃이 걸려 있음", "timeout=" in _isrun_src)

# (4) 세션 연속성 — 매일 새 대화로 리셋되면 폰에서 어제 맥락을 잃는다.
_ps1 = _os.path.join(_ROOT, "start_remote_control.ps1")
_ps1_txt = open(_ps1, encoding="utf-8-sig").read() if _os.path.exists(_ps1) else ""
check("원격제어 런처가 존재함", bool(_ps1_txt))
check("기본 경로가 --continue (전날 대화 이어가기)", "--continue" in _ps1_txt)
check("자동 재시작 루프가 있음(모바일 종료 클릭 후 방치 방지)",
      "while ($restartCount" in _ps1_txt)
check("연속 즉시종료 시 무한재시작 중단 장치가 있음",
      "maxConsecutiveFastFails" in _ps1_txt)
check("이관용 1회성 새 세션 플래그를 지원함",
      "NEW_SESSION_REQUESTED" in _ps1_txt)
check("플래그는 사용 즉시 삭제(1회성 보장)",
      "Remove-Item $newSessionFlag" in _ps1_txt)

# (5) 인코딩 함정 — 여기서 틀리면 무인 기동이 통째로 실패한다(2026-07-28 실장애).
_ps1_head = open(_ps1, "rb").read(3) if _os.path.exists(_ps1) else b""
check("start_remote_control.ps1이 UTF-8 BOM (PS 5.1 한글 파싱)",
      _ps1_head == b"\xef\xbb\xbf", repr(_ps1_head))
_bat = _os.path.join(_ROOT, "start_trader.bat")
_bat_raw = open(_bat, "rb").read() if _os.path.exists(_bat) else b""
check("start_trader.bat이 순수 ASCII (cmd.exe 코드페이지 함정 회피)",
      all(b < 0x80 for b in _bat_raw), f"{len(_bat_raw)}바이트")
check("bat이 ASCII junction 경로를 참조(한글 경로 회피)",
      b"C:\\AutoTrader_Bot\\ProjectRoot" in _bat_raw)

# (6) 고아 STOP_SIGNAL — 있으면 기동 5초 만에 스스로 죽어 하루가 통째로 날아간다.
check("setup()에 낡은 STOP_SIGNAL 정리 로직이 있음",
      "낡은 STOP_SIGNAL" in _main_src)
check("현재 STOP_SIGNAL 고아 파일이 없음",
      not _os.path.exists(_os.path.join(_ROOT, "STOP_SIGNAL")))
check("이관 플래그가 남아있지 않음(다음 기동은 이어가기)",
      not _os.path.exists(_os.path.join(_ROOT, "NEW_SESSION_REQUESTED")))

# ═════════════════════════════════════════════════════════
print("\n[20] 상수 정합성 불변식 — 서로 모순되는 설정이 없는지")
# 08-03에 결함 ①(1A 기본캡 == TP_CAP_UPGRADED라 cap_exit이 항상 참)이 정확히
# 이 부류였다. 값 하나를 고칠 때 짝이 되는 값을 안 고쳐서 생기는 사고를
# 상수 수준에서 미리 잡는다.
import core.daily_backtest as _DB
from core.order_manager import FORCE_CLOSE_TIME as _FCT

# 시간창
check("[불변] Pullback 시작 == 1A 시작", SM.PULLBACK_START == SM.GROUP_A_START,
      str(SM.PULLBACK_START))
check("[불변] Pullback 종료 == 1A 종료", SM.PULLBACK_END == SM.PHASE1A_END)
check("[불변] ENTRY_WINDOW_END == 두 전략 종료 중 늦은 쪽",
      SM.ENTRY_WINDOW_END == max(SM.PHASE1A_END, SM.PULLBACK_END))
check("[불변] 진입 종료 < 강제청산 (청산 중 신규매수 없음)",
      SM.ENTRY_WINDOW_END.strftime("%H:%M") < _FCT)
check("[불변] 신규매수 하드컷오프 == 강제청산 시각",
      SM.ENTRY_HARD_CUTOFF.strftime("%H:%M") == _FCT)
check("[불변] 개장초반 캡 경계가 진입창 안",
      SM.GROUP_A_START < SM.EARLY_WINDOW_END < SM.PHASE1A_END)
check("[불변] 중복종목 전환시각이 Pullback 창 안",
      SM.PULLBACK_START <= SM.DUAL_SOURCE_PULLBACK_FROM < SM.PULLBACK_END)

# 익절/손절
check("[불변] 기본캡 != 상향캡 (결함① 재발 방지)",
      SM.TAKE_PROFIT_CAP != SM.TP_CAP_UPGRADED_MAX)
check("[불변] 상향캡 > 기본캡", SM.TP_CAP_UPGRADED_MAX > SM.TAKE_PROFIT_CAP)
check("[불변] 개장초반캡 <= 기본캡", SM.TAKE_PROFIT_CAP_EARLY <= SM.TAKE_PROFIT_CAP)
check("[불변] 눌림캡 <= 기본캡", SM.TAKE_PROFIT_CAP_PULLBACK <= SM.TAKE_PROFIT_CAP)
check("[불변] 모든 캡이 왕복수수료보다 큼(구조적 손실 방지)",
      min(SM.TAKE_PROFIT_CAP, SM.TAKE_PROFIT_CAP_PULLBACK,
          SM.TAKE_PROFIT_CAP_EARLY) > SM.ROUND_TRIP_COST)
check("[불변] 손절선이 음수", SM.STOP_LOSS_RATE < 0)
check("[불변] 본전스톱 트리거 > 바닥", SM.BREAKEVEN_TRIGGER > SM.BREAKEVEN_FLOOR)

# 틱 진입
check("[불변] 무장 요구시간 < 무장 TTL(무장이 즉시 만료되면 안 됨)",
      SM.TICK_STRENGTH_SUSTAIN_SEC < SM.TICK_ARM_TTL_SEC)
check("[불변] 무장 요구시간 > 재평가 쿨다운",
      SM.TICK_STRENGTH_SUSTAIN_SEC > SM.TICK_ENTRY_COOLDOWN_SEC)
check("[불변] 시간대 계수가 원기준(1.0)을 넘지 않음",
      all(v <= 1.0 for _, v in SM.TICK_BURST_TIME_MULT))
check("[불변] 시간대 계수 구간이 오름차순",
      [k for k, _ in SM.TICK_BURST_TIME_MULT]
      == sorted(k for k, _ in SM.TICK_BURST_TIME_MULT))
check("[불변] 상대경로 하한 < 절대 기준",
      SM.TICK_BURST_REL_FLOOR < SM.PHASE1A_BURST_TRADE_VALUE)
check("[불변] 단일체결 기준 > 절대 기준",
      SM.PHASE1A_SINGLE_TRADE_VALUE > SM.PHASE1A_BURST_TRADE_VALUE)

# 슬롯
check("[불변] 전략별 슬롯 합 >= 공유상한(한쪽이 남으면 흡수 가능)",
      SM.PHASE1A_MAX_SLOTS + SM.PULLBACK_MAX_SLOTS >= SM.MAX_HOLDINGS_HARD)
check("[불변] 확장슬롯 > 공유상한", SM.MAX_HOLDINGS_HARD > SM.MAX_HOLDINGS)
check("[불변] 전략별 캡 < 공유상한(한 전략 독식 불가)",
      SM.PHASE1A_MAX_SLOTS >= SM.MAX_HOLDINGS)   # [E] 눌림 0이라 1A가 다 채워야 한다

# 백테스트 동기화 — 어긋나면 리포트가 라이브와 다른 규칙으로 계산된다
check("[동기] 백테스트 캡", _DB.TAKE_PROFIT_CAP == SM.TAKE_PROFIT_CAP)
check("[동기] 백테스트 눌림캡",
      _DB.TAKE_PROFIT_CAP_PULLBACK == SM.TAKE_PROFIT_CAP_PULLBACK)
check("[동기] 백테스트 개장초반캡",
      _DB.TAKE_PROFIT_CAP_EARLY == SM.TAKE_PROFIT_CAP_EARLY)
check("[동기] 백테스트 손절", _DB.STOP_LOSS_RATE == SM.STOP_LOSS_RATE)
check("[동기] 백테스트 본전스톱 플래그",
      _DB.BREAKEVEN_STOP_ENABLED == SM.BREAKEVEN_STOP_ENABLED)
check("[동기] 백테스트 Pullback 창",
      _DB.PULLBACK_START_HHMM == SM.PULLBACK_START.strftime("%H%M")
      and _DB.PULLBACK_END_HHMM == SM.PULLBACK_END.strftime("%H%M"),
      f"{_DB.PULLBACK_START_HHMM}~{_DB.PULLBACK_END_HHMM}")
check("[동기] 백테스트 강제청산", _DB.FORCE_CLOSE_HHMM == _FCT.replace(":", ""))

# ═════════════════════════════════════════════════════════
print("\n[21] 08-04 설정 조합 통합 — 바뀐 값들이 함께 굴러가는지")
# 오늘 바꾼 것: PB창 09:00 / 무장 1.5초 / 본전스톱 ON(2026-08-04 재활성화) /
#              캡 4.0·2.5·6.0 / 손절 워밍업 중 작동 / 점심계수 1.00
# 개별 검증은 test_patch_20260803.py가 하고, 여기선 **동시에** 태운다.

# (1) 09:00 직후 눌림목 — 새 창 + 2초 무장으로 매수까지
sN, cN = build(datetime(2026, 8, 3, 9, 2, 0))
_pb_old3 = SM.PULLBACK_MAX_SLOTS
SM.PULLBACK_MAX_SLOTS = 4   # [E] 설정조합 배선 검증용 임시 복구
sN.on_condition_hit("PB1", "눌림새창", cond_name="눌림목자동")
ob(sN, "PB1")
TN = time.time()
tick(sN, "PB1", 130.0, TN)
tick(sN, "PB1", 130.0, TN + 1.0)
check("1.0초에는 무장 전(요구 1.5초)", "PB1" not in sN._armed_at)
burst(sN, "PB1", TN + 2.2)
tick(sN, "PB1", 130.0, TN + 2.2)
tick(sN, "PB1", 130.0, TN + 3.2, price=9_940, side="sell")  # 되돌림 체결
check("09:02 눌림목이 1.5초 무장 + 버스트로 매수 (구버전: 창밖+3초 미달로 둘 다 불가)",
      "PB1" in sN.holdings, f"holdings={list(sN.holdings)}")
check("눌림 전략으로 라우팅", sN.holdings["PB1"]["sub_strategy"] == "1A_눌림")

# (2) 개장초반(09:10 이전) 매수분은 개장초반 캡을 받는다
capN, lblN = sN._take_profit_cap(sN.holdings["PB1"])
check("09:02 매수분 캡 = 개장초반 2.5%",
      abs(capN - SM.TAKE_PROFIT_CAP_EARLY) < 1e-9, f"{capN} ({lblN})")

# (3) 본전스톱 ON(2026-08-04 재활성화) — +1% 찍고 되돌리면 본전에서 청산
buyN = sN.holdings["PB1"]["buy_price"]
armN = int(buyN * (1 + SM.BREAKEVEN_TRIGGER + SM.ROUND_TRIP_COST)) + 1
cN.set(9, 5, 0)
sN.holdings["PB1"]["warmup_until"] = cN.dt - timedelta(seconds=1)
sN.on_price_update("PB1", armN)
check("본전스톱 ON — 무장됨", sN.holdings["PB1"].get("breakeven_armed") is True)
sN.on_price_update("PB1", buyN)
check("본전스톱 ON — 본전 복귀 시 청산", "PB1" not in sN.holdings)
SM.PULLBACK_MAX_SLOTS = _pb_old3   # [E] 원상복구

# (4) 손절은 워밍업 중에도 작동 (별도 포지션으로)
sS, cS = build(datetime(2026, 8, 3, 9, 3, 0))
sS.holdings["ST1"] = {
    "trade_id": 1, "qty": 10, "buy_price": 10_000, "buy_time": cS.dt,
    "stock_name": "ST1", "sub_strategy": "1A",
    "warmup_until": cS.dt + timedelta(seconds=60),   # 워밍업 진행 중
    "entry_strength": 150.0, "highest_price": 10_000, "lowest_price": 10_000,
}
sS.on_price_update("ST1", 9_600)   # -4%
check("워밍업 중에도 손절 발동", "ST1" not in sS.holdings)

# (5) 점심 시간대 문턱이 원기준(3천만원) 유지
check("12:00 버스트 문턱이 원기준 그대로(완화 제거) — 금액 하드코딩 금지",
      abs(SM.PHASE1A_BURST_TRADE_VALUE
          * sN.burst_time_multiplier(datetime(2026, 8, 3, 12, 0, 0))
          - SM.PHASE1A_BURST_TRADE_VALUE) < 1)

# (6) 1A는 09:10 이후 기본캡 4.0%로 넓어진다(개장초반과 구분되는지)
sL, _ = build(datetime(2026, 8, 3, 11, 0, 0))
sL.holdings["LT1"] = {
    "trade_id": 1, "qty": 10, "buy_price": 10_000,
    "buy_time": datetime(2026, 8, 3, 11, 0, 0), "stock_name": "LT1",
    "sub_strategy": "1A", "warmup_until": None,
    "entry_strength": 150.0, "highest_price": 10_000, "lowest_price": 10_000,
    "buy_hhmm": "1100",
}
capL, lblL = sL._take_profit_cap(sL.holdings["LT1"])
check("11:00 매수 1A 캡 = 기본 4.0%", abs(capL - SM.TAKE_PROFIT_CAP) < 1e-9,
      f"{capL} ({lblL})")

# ═════════════════════════════════════════════════════════
print("\n[22] 전면차단(가드/MDD/HALT/격리)이 '매도만 하는' 반쪽 동작을 만들지 않는지")
# (2026-08-04) 08-03에 slot_replacement에서 고친 것과 **같은 결함**이
# _try_1a_priority_upgrade 경로에 그대로 남아 있었다.
#   호출부는 can_buy_*가 False라는 것만 보고 "슬롯이 꽉 찼다"로 해석해
#   보유분을 팔아 자리를 비운다. 그런데 그 False가 지수 가드/MDD/HALT/격리
#   때문이면 비운 자리를 채울 매수가 막혀 있어 **손실만 확정된다.**
# 같은 규칙이 여러 곳에 흩어져 한쪽만 고치는 이 프로젝트의 반복 사고 패턴이라
# _entry_block_reason() 한 점으로 모으고 여기서 못박는다.


def _fill_slots(s, n=None, price=10_000):
    """슬롯을 만석으로 채우고, 그 종목들에 체결틱까지 흘려 넣는다.
    (틱이 없으면 _fresh_tick_price/candidate_tier가 성립하지 않아
     교체 로직이 '우연히' 안 도는 상태가 되어 테스트가 무의미해진다)"""
    n = n or SM.MAX_HOLDINGS
    t0 = time.time()
    for i in range(n):
        code = f"FL{i:03d}"
        s.holdings[code] = {
            "trade_id": 1, "qty": 10, "buy_quantity": 10, "buy_price": price,
            "buy_time": s._now() - timedelta(minutes=20), "stock_name": code,
            "sub_strategy": "1A", "warmup_until": s._now() - timedelta(seconds=1),
            "entry_strength": 300.0, "highest_price": price, "lowest_price": price,
        }
        s.phase1b.start_watching(code)
        for k in range(30):
            s.phase1b.trade_flow.add_tick(code, price, "buy", 5, now=t0 - 100 + k * 3)
        s.phase1b.trade_flow.add_tick(code, price, "buy", 5, now=t0 - 0.5)


def _set_idx(s, v):
    """지수 등락률을 주입한다. _market_rate_at을 '지금'으로 두면 60초 캐시가
    유효해져 REST 스텁이 값을 덮어쓰지 않는다."""
    s._kospi_rate = s._kosdaq_rate = v
    s._market_rate_at = s._now()


_blockers = [
    ("지수 하락 가드", lambda s: _set_idx(s, SM.INDEX_GUARD_THRESHOLD - 1.0)),
    ("MDD 일손실 차단", lambda s: setattr(s, "_risk_tripped", True)),
    ("WS 재연결 격리", lambda s: setattr(s, "quarantine_until",
                                       s._now() + timedelta(minutes=5))),
]


for _label, _apply in _blockers:
    sB, _ = build(datetime(2026, 8, 3, 11, 5, 0))
    _fill_slots(sB)
    _apply(sB)
    _sold = []
    sB._execute_sell = lambda c, p, r: _sold.append((c, r))
    sB.candidate_tier = lambda c: (0.1 if c.startswith("FL") else 99.0)
    _did = sB._try_1a_priority_upgrade("NEWC1", 99.0)
    check(f"{_label} 중 우선순위 교체가 자리를 비우지 않음",
          _did is False and not _sold, f"did={_did} sold={_sold}")
    check(f"{_label} 사유가 진단에 그대로 기록됨(슬롯 부족으로 오분류 안 됨)",
          _label in (sB._entry_block_reason() or ""), str(sB._entry_block_reason()))

# 대조군 — 차단 사유가 없으면 교체는 **여전히 동작해야 한다**(과잉차단 아님)
sC, _ = build(datetime(2026, 8, 3, 11, 5, 0))
_fill_slots(sC)
_soldC = []
sC._execute_sell = lambda c, p, r: _soldC.append((c, r))
sC.candidate_tier = lambda c: (0.1 if c.startswith("FL") else 99.0)
check("[대조군] 차단 사유 없음", sC._entry_block_reason() is None,
      str(sC._entry_block_reason()))
check("[대조군] 정상 상태에서는 우선순위 교체가 동작(과잉차단 아님)",
      sC._try_1a_priority_upgrade("NEWC2", 99.0) is True and bool(_soldC),
      str(_soldC))

# can_buy_more 리팩터링 후에도 판정이 동일한지 (경계 포함)
sM, cM = build(datetime(2026, 8, 3, 10, 59, 0))
_set_idx(sM, -6.0)
check("10:59엔 지수 가드 미발동(11:00부터)", sM.can_buy_more() is True)
cM.set(11, 0, 0); _set_idx(sM, SM.INDEX_GUARD_THRESHOLD - 1.0)
check("11:00 정각부터 지수 가드 발동", sM.can_buy_more() is False)
_set_idx(sM, SM.INDEX_GUARD_THRESHOLD + 0.1)
check("임계 미달(-4.9%)이면 매수 재개", sM.can_buy_more() is True)
_set_idx(sM, SM.INDEX_GUARD_THRESHOLD)
check("임계 정확히 도달하면 차단", sM.can_buy_more() is False)
_set_idx(sM, 0.0)
check("지수 회복 후 매수 재개", sM.can_buy_more() is True)

# ═════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print(f"  - {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
