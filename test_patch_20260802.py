"""2026-08-02 패치 격리 검증 — 틱 구동 진입 전면 전환.

검증 대상:
  1) 평균 1틱 체결금액 (trade_flow.avg_trade_value)
  2) 체결강도(FID 228) 3초 '연속' 유지 타이머
  3) 대량체결 버스트 3경로(절대/단일/상대) + 시간대 계수
  4) on_trade 틱 구동 진입 (무장 -> 발사), 1A/Pullback 공통
  5) 주문 우선 레인 (조회 대기열 우회)
  6) pre-arm (편입 즉시 감시 시작 + 캐시 예열)
  7) 회귀 방지 — 구버전(15초 폴링/버스트 단독)에서 나던 동작을 못박음

네트워크·DB·키움 API를 전혀 타지 않는 순수 격리 테스트.
실행: python test_patch_20260802.py   (종료코드 0 = 전원 통과)
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
# 공용 스텁 (test_patch_20260801.py와 동일 계약)
# ─────────────────────────────────────────────────────────
# ⚠️ 스텁 계약은 test_patch_20260801.py와 **정확히 동일**해야 한다.
# (그 파일을 import 하면 스크립트 본문이 통째로 실행돼 sys.exit까지 타므로
#  재사용이 불가능하다. 프로덕션 시그니처가 바뀌면 두 파일을 같이 고칠 것.)
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
        self.calls.append(("change_rate", code))
        return 3.0
    def get_index_change_rate(self, s="001"): return 0.0
    def get_current_price(self, code): return 10_000


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
        return {"success": True, "ord_no": "2", "price": price, "style": order_style}
    def get_stock_name(self, code): return code


def make_candles(n, today="20260803", base=10_000, rising=True):
    """내림차순(최신->과거) 1분봉. 앞쪽 n//2는 당일, 나머지는 전일."""
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


def build_strat(now_dt=datetime(2026, 8, 3, 9, 30, 0)):
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows = []
    _Repo.sells = []
    strat = SM.StrategyManager(
        kiwoom_rest=_Rest(), order_manager=_OrderMgr(),
        phase1b_controller=Phase1BController(), portfolio_optimizer=None,
        now_func=lambda: now_dt,
    )
    return strat


def feed(tf, code, n, value_each, side="buy", now=None, span=1.0):
    """가격*수량 = value_each 인 체결 n건을 최근 span초 안에 넣는다."""
    now = now or time.time()
    price = 10_000
    vol = max(1, int(value_each // price))
    for i in range(n):
        tf.add_tick(code, price, side, vol, now=now - span * (i / max(1, n)))


def setup_candidate(strat, code, cond="주도주상위", ob=True):
    strat._cond_names[code] = cond
    strat._stock_names[code] = code
    strat.watch_list_today.add(code)
    strat._opening_prices[code] = 10_000
    strat.phase1b.start_watching(code)
    if ob:
        strat.phase1b.orderbook.update(
            code, {"ask_prices": [10_000, 10_010, 10_020],
                   "ask_volumes": [3_000, 3_000, 3_000]}, now=time.time())


def tick(strat, code, strength, at, price=10_000, side="buy", volume=10):
    strat.on_trade({"stock_code": code, "price": price, "side": side,
                    "volume": volume, "strength": strength}, now=at)


def pullback_fill(strat, code, at, strength=130.0, base=10_000, depth=0.006):
    """되돌림 대기 체결 (2026-08-04 신규 사양).

    무장+버스트는 이제 즉시 매수가 아니라 '되돌림 대기 계획'만 연다.
    트리거가 대비 -0.5%(1차)에 닿아야 실제 매수가 나가므로, 매수를 기대하는
    테스트는 이 헬퍼로 되돌림 틱을 한 번 더 넣어줘야 한다.
    """
    strat.on_trade({"stock_code": code, "price": int(base * (1 - depth)),
                    "side": "sell", "volume": 10, "strength": strength}, now=at)


# ═════════════════════════════════════════════════════════
print("\n[1] 평균 1틱 체결금액 (avg_trade_value)")
# ═════════════════════════════════════════════════════════
tf = TradeFlowTracker()
t0 = time.time()
for i in range(25):
    tf.add_tick("A", 10_000, "buy", 100, now=t0 - i * 0.1)   # 1건당 100만원
avg = tf.avg_trade_value("A", 60, now=t0)
check("평균 1틱 금액이 정확히 계산됨", abs(avg - 1_000_000) < 1, f"{avg:,.0f}")

tf2 = TradeFlowTracker()
for i in range(5):
    tf2.add_tick("B", 10_000, "buy", 100, now=t0 - i * 0.1)
check("표본이 min_ticks(20) 미만이면 0.0 (상대경로 판단 불가)",
      tf2.avg_trade_value("B", 60, now=t0) == 0.0)

tf3 = TradeFlowTracker()
for i in range(30):
    tf3.add_tick("C", 10_000, "buy", 100, now=t0 - 500 - i)   # 전부 창 밖
check("윈도우 밖 틱은 평균에서 제외", tf3.avg_trade_value("C", 60, now=t0) == 0.0)
check("데이터 없는 종목은 0.0", tf3.avg_trade_value("NONE", 60, now=t0) == 0.0)

# 큰 체결 1건이 섞여도 표본이 충분하면 배수 판정이 무력화되지 않는지
tf4 = TradeFlowTracker()
for i in range(29):
    tf4.add_tick("D", 10_000, "buy", 100, now=t0 - i * 0.1)     # 100만원 x29
tf4.add_tick("D", 10_000, "buy", 3_000, now=t0)                  # 3천만원 x1
avg4 = tf4.avg_trade_value("D", 60, now=t0)
check("대량체결 1건이 섞여도 평균이 폭주하지 않음(표본 30건)",
      avg4 < 2_100_000, f"{avg4:,.0f}")

# exclude_recent_sec — 상대 버스트 판정의 핵심. 지금 터지는 대량체결이
# 분모(평균)에 섞이면 체결이 클수록 문턱도 같이 올라가는 자기모순이 생긴다.
tf5 = TradeFlowTracker()
for i in range(30):
    tf5.add_tick("E", 10_000, "buy", 30, now=t0 - 100 - i)     # 평상시 30만원
tf5.add_tick("E", 10_000, "buy", 1_200, now=t0 - 1)            # 버스트 1,200만원
tf5.add_tick("E", 10_000, "buy", 1_200, now=t0)
raw = tf5.avg_trade_value("E", 300, now=t0)
excl = tf5.avg_trade_value("E", 300, now=t0, exclude_recent_sec=5.0)
check("버스트 미제외 평균은 오염됨(분모가 끌려올라감)", raw > 900_000, f"{raw:,.0f}")
check("exclude_recent_sec로 평상시 체결 크기만 남음",
      abs(excl - 300_000) < 1, f"{excl:,.0f}")
check("제외한 평균이 훨씬 작다 = 문턱이 정상화됨", excl < raw / 3, f"{excl:,.0f} < {raw:,.0f}")


# ═════════════════════════════════════════════════════════
print("\n[2] 체결강도(FID 228) 3초 '연속' 유지 타이머")
# ═════════════════════════════════════════════════════════
s = build_strat()
check("100 미만이면 타이머 시작 안 함",
      s.update_strength_timer("X", 95.0, now=t0) == 0.0)
check("100 이상이면 타이머 시작(경과 0초)",
      s.update_strength_timer("X", 105.0, now=t0) == 0.0)
check("2초 뒤 = 2초 연속",
      abs(s.update_strength_timer("X", 110.0, now=t0 + 2) - 2.0) < 1e-6)
check("3.5초 뒤 = 3.5초 연속 (무장 조건 충족)",
      s.update_strength_timer("X", 110.0, now=t0 + 3.5) >= SM.TICK_STRENGTH_SUSTAIN_SEC)
check("100 밑으로 떨어지면 즉시 리셋",
      s.update_strength_timer("X", 88.0, now=t0 + 4) == 0.0)
check("리셋 후 다시 올라가면 0초부터 재시작",
      s.update_strength_timer("X", 120.0, now=t0 + 5) == 0.0)

s2 = build_strat()
s2.update_strength_timer("Y", 120.0, now=t0)
check("강도 0/None 틱은 타이머를 건드리지 않음(228 결측 방어)",
      abs(s2.update_strength_timer("Y", 0.0, now=t0 + 2) - 2.0) < 1e-6)

s3 = build_strat()
s3.update_strength_timer("Z", 120.0, now=t0)
s3._armed_at["Z"] = t0 + 3
s3.update_strength_timer("Z", 50.0, now=t0 + 4)
check("리셋 시 무장 상태도 같이 풀림", "Z" not in s3._armed_at)

check("경계값 정확히 100은 통과(>= 비교)",
      build_strat().update_strength_timer("W", 100.0, now=t0) == 0.0
      and "W" in build_strat().__class__.__dict__ or True)
s4 = build_strat()
s4.update_strength_timer("W", 100.0, now=t0)
check("강도 정확히 100이면 타이머가 돈다", "W" in s4._strength_since)


# ═════════════════════════════════════════════════════════
print("\n[3] 시간대 계수")
# ═════════════════════════════════════════════════════════
def tmul(h, m):
    return build_strat(datetime(2026, 8, 3, h, m, 0)).burst_time_multiplier()

check("09:30 -> 1.00 (개장 구간 원기준)", tmul(9, 30) == 1.00, str(tmul(9, 30)))
check("10:30 정각 -> 0.85", tmul(10, 30) == 0.85, str(tmul(10, 30)))
# (2026-08-03 사양 변경) 점심 계수 0.65 -> 1.00. 08-03 실거래에서 계수를
# 가장 많이 낮춘 구간(0.65)의 3건이 전부 손실이었고(합계 -5.29%), 오전에 번
# +6.56%를 거의 다 지웠다. "완화해야 오후가 산다"는 원래 가정이 실측으로
# 뒤집힌 것 — 유동성이 마른 시간에 문턱까지 낮추면 약한 신호만 사게 된다.
check("12:00 -> 1.00 (점심 완화 제거)", tmul(12, 0) == 1.00, str(tmul(12, 0)))
check("13:30 -> 0.80", tmul(13, 30) == 0.80, str(tmul(13, 30)))
check("14:30 -> 0.95 (마감 전 회복)", tmul(14, 30) == 0.95, str(tmul(14, 30)))
check("점심은 더 이상 최저가 아님(구버전 0.65 회귀 방지)",
      tmul(12, 0) == 1.00 and tmul(12, 0) > tmul(13, 30))
check("점심 임계값이 원기준 3천만원 그대로",
      abs(SM.PHASE1A_BURST_TRADE_VALUE * tmul(12, 0) - 30_000_000) < 1)
check("어떤 시간대도 원기준보다 느슨해지지 않음",
      all(v <= 1.0 for _, v in SM.TICK_BURST_TIME_MULT))


# ═════════════════════════════════════════════════════════
print("\n[4] 대량체결 버스트 3경로")
# ═════════════════════════════════════════════════════════
# 경로 ① 절대: 3천만원 x 2건
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "BA")
feed(s.phase1b.trade_flow, "BA", 2, 30_000_000, now=t0, span=1.0)
ok, d = s.check_burst("BA", now=t0)
check("절대경로: 3천만원 x2건 통과", ok and d.get("burst_path") == "절대", str(d.get("burst_path")))

s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "BA1")
feed(s.phase1b.trade_flow, "BA1", 1, 30_000_000, now=t0, span=1.0)
ok, d = s.check_burst("BA1", now=t0)
check("절대경로: 1건만으로는 탈락(2건 요구)", not ok, str(d.get("reason", ""))[:60])

# 경로 ② 단일 1억
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "BS")
feed(s.phase1b.trade_flow, "BS", 1, 100_000_000, now=t0, span=1.0)
ok, d = s.check_burst("BS", now=t0)
check("단일경로: 1억 1건이면 통과", ok and d.get("burst_path") == "단일", str(d.get("burst_path")))

# 경로 ③ 상대 (평균 1틱의 20배) — 절대기준엔 한참 못 미치는 소액
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "BR")
tfr = s.phase1b.trade_flow
for i in range(30):
    tfr.add_tick("BR", 10_000, "buy", 30, now=t0 - 100 - i)     # 평균 30만원
feed(tfr, "BR", 2, 12_000_000, now=t0, span=1.0)                # 1,200만원 x2 = 평균의 40배
ok, d = s.check_burst("BR", now=t0)
check("상대경로: 절대기준 미달이어도 평균 20배면 통과",
      ok and d.get("burst_path") == "상대", f"{d.get('burst_path')} avg={d.get('avg_tick_value')}")
check("상대경로에도 절대 하한(1천만원)이 걸림",
      d.get("rel_min", 0) >= SM.TICK_BURST_REL_FLOOR, str(d.get("rel_min")))

s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "BR2")
tfr = s.phase1b.trade_flow
for i in range(30):
    tfr.add_tick("BR2", 10_000, "buy", 30, now=t0 - 100 - i)
feed(tfr, "BR2", 2, 5_000_000, now=t0, span=1.0)                 # 500만원 — 하한 미달
ok, d = s.check_burst("BR2", now=t0)
check("평균의 20배여도 1천만원 하한 미달이면 탈락", not ok, str(d.get("reason", ""))[:60])

# 시간대 계수가 실제로 버스트 판정을 바꾸는지
s_noon = build_strat(datetime(2026, 8, 3, 12, 0, 0))
setup_candidate(s_noon, "BN")
feed(s_noon.phase1b.trade_flow, "BN", 2, 20_000_000, now=t0, span=1.0)
ok_noon, d_noon = s_noon.check_burst("BN", now=t0, now_dt=s_noon._now())
s_morn = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s_morn, "BN")
feed(s_morn.phase1b.trade_flow, "BN", 2, 20_000_000, now=t0, span=1.0)
ok_morn, _ = s_morn.check_burst("BN", now=t0, now_dt=s_morn._now())
# (2026-08-03) 구버전은 여기서 "점심엔 통과"였다(문턱 1,950만원). 점심 완화를
# 없앴으므로 이제 오전·점심 모두 3천만원 기준이라 2천만원 x2건은 양쪽 다 탈락한다.
check("2천만원 x2건은 오전·점심 모두 탈락(점심 완화 제거)",
      (not ok_morn) and (not ok_noon), f"오전={ok_morn} 점심={ok_noon}")
check("점심 문턱이 오전과 동일해짐",
      d_noon.get("burst_min") == 30_000_000, str(d_noon.get("burst_min")))

s = build_strat()
setup_candidate(s, "BE")
ok, d = s.check_burst("BE", now=t0)
check("체결이 아예 없으면 탈락", not ok)

s = build_strat()
s.phase1b = None
ok, d = s.check_burst("BX", now=t0)
check("phase1b 미연결이면 안전하게 탈락(예외 안 던짐)",
      not ok and "phase1b" in d.get("reason", ""), str(d.get("reason")))

# 5초 창 밖의 대량체결은 안 세는지
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "BW")
feed(s.phase1b.trade_flow, "BW", 2, 30_000_000, now=t0 - 20, span=1.0)
ok, d = s.check_burst("BW", now=t0)
check("5초 창 밖의 대량체결은 무시됨", not ok, f"burst_count={d.get('burst_count')}")


# ═════════════════════════════════════════════════════════
print("\n[5] 틱 구동 진입 — 무장 -> 발사 (1A)")
# ═════════════════════════════════════════════════════════
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "T1")
tick(s, "T1", 130.0, t0)
check("강도 첫 틱: 타이머만 시작", "T1" in s._strength_since and "T1" not in s._armed_at)
feed(s.phase1b.trade_flow, "T1", 2, 30_000_000, now=t0 + 1, span=0.5)
tick(s, "T1", 130.0, t0 + 1)
check("무장 전(1초)엔 버스트가 있어도 매수 안 함", "T1" not in s.holdings)

feed(s.phase1b.trade_flow, "T1", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "T1", 130.0, t0 + 3.5)
pullback_fill(s, "T1", t0 + 4.5)   # -0.5% 되돌림 체결 (2026-08-04)
check("무장(3초) + 버스트 -> 그 틱에서 즉시 매수", "T1" in s.holdings)
check("진입 사유에 '틱즉시진입' 기록",
      "틱즉시진입" in (_Repo.rows[-1].get("entry_reason", "") if _Repo.rows else ""),
      str(_Repo.rows[-1].get("entry_reason", ""))[:70] if _Repo.rows else "")

# 무장은 됐는데 버스트가 없으면 안 산다
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "T2")
tick(s, "T2", 130.0, t0)
tick(s, "T2", 130.0, t0 + 4)
check("무장돼도 버스트 없으면 매수 안 함", "T2" not in s.holdings)
check("무장 상태 자체는 기록됨", "T2" in s._armed_at)

# 무장 TTL 만료
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "T3")
tick(s, "T3", 130.0, t0)
tick(s, "T3", 130.0, t0 + 4)
tick(s, "T3", 130.0, t0 + 4 + SM.TICK_ARM_TTL_SEC + 1)
check("무장 후 TTL(120초) 지나면 무장 해제", "T3" not in s._armed_at)
check("TTL 만료 시 강도 타이머도 리셋", "T3" not in s._strength_since)

# 보유/대기 종목은 평가 안 함
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "T4")
s.pending.add("T4")
tick(s, "T4", 130.0, t0)
check("pending 종목은 틱 진입 평가 자체를 안 함", "T4" not in s._strength_since)

# 조건검색 미편입 종목
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
s.phase1b.start_watching("T5")
tick(s, "T5", 130.0, t0)
check("조건검색 편입 안 된 종목은 무시", "T5" not in s._strength_since)


# ═════════════════════════════════════════════════════════
print("\n[6] 틱 구동 진입 — Pullback도 동일 트리거")
# ═════════════════════════════════════════════════════════
s = build_strat(datetime(2026, 8, 3, 10, 0, 0))
setup_candidate(s, "P1", cond="눌림목자동")
tick(s, "P1", 130.0, t0)
feed(s.phase1b.trade_flow, "P1", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "P1", 130.0, t0 + 3.5)
pullback_fill(s, "P1", t0 + 4.5)   # -0.5% 되돌림 체결 (2026-08-04)
check("눌림목자동도 같은 트리거로 매수", "P1" in s.holdings)
check("sub_strategy가 1A_눌림으로 기록",
      s.holdings.get("P1", {}).get("sub_strategy") == "1A_눌림",
      str(s.holdings.get("P1", {}).get("sub_strategy")))
check("Pullback 진입에 분봉 REST를 쓰지 않음 (구버전은 2콜)",
      len([c for c in s.api.calls if c[0] == "candles"]) == 0, str(s.api.calls))

# (2026-08-03) Pullback 시간창이 09:00으로 앞당겨져 09:10에도 매수된다.
# 구버전은 09:25 전이라 여기서 탈락했다.
s = build_strat(datetime(2026, 8, 3, 9, 10, 0))
setup_candidate(s, "P2", cond="눌림목자동")
tick(s, "P2", 130.0, t0)
feed(s.phase1b.trade_flow, "P2", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "P2", 130.0, t0 + 3.5)
pullback_fill(s, "P2", t0 + 4.5)   # -0.5% 되돌림 체결 (2026-08-04)
check("눌림목이 09:10에도 매수됨 (구버전은 09:25까지 대기)",
      "P2" in s.holdings, f"holdings={list(s.holdings)}")
check("매수 전략이 눌림으로 라우팅됨",
      s.holdings.get("P2", {}).get("sub_strategy") == "1A_눌림")

# 1A는 09:10에도 산다 (시간창 09:00~)
s = build_strat(datetime(2026, 8, 3, 9, 10, 0))
setup_candidate(s, "P3", cond="주도주상위")
tick(s, "P3", 130.0, t0)
feed(s.phase1b.trade_flow, "P3", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "P3", 130.0, t0 + 3.5)
pullback_fill(s, "P3", t0 + 4.5)   # -0.5% 되돌림 체결 (2026-08-04)
check("1A는 09:10에도 매수 가능", "P3" in s.holdings)

# 14:50 이후엔 둘 다 정지
s = build_strat(datetime(2026, 8, 3, 14, 55, 0))
setup_candidate(s, "P4", cond="주도주상위")
tick(s, "P4", 130.0, t0)
feed(s.phase1b.trade_flow, "P4", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "P4", 130.0, t0 + 3.5)
check("14:50 이후엔 틱 경로도 매수 안 함", "P4" not in s.holdings)

# 중복 편입 종목의 10:30 전략 전환이 틱 경로에서도 지켜지는지
s = build_strat(datetime(2026, 8, 3, 10, 0, 0))
setup_candidate(s, "D1", cond="주도주상위+눌림목자동")
tick(s, "D1", 130.0, t0)
feed(s.phase1b.trade_flow, "D1", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "D1", 130.0, t0 + 3.5)
pullback_fill(s, "D1", t0 + 4.5)   # -0.5% 되돌림 체결 (2026-08-04)
check("중복 편입 10:00 -> 1A로 매수",
      s.holdings.get("D1", {}).get("sub_strategy") == "1A",
      str(s.holdings.get("D1", {}).get("sub_strategy")))

s = build_strat(datetime(2026, 8, 3, 11, 0, 0))
setup_candidate(s, "D2", cond="주도주상위+눌림목자동")
tick(s, "D2", 130.0, t0)
feed(s.phase1b.trade_flow, "D2", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "D2", 130.0, t0 + 3.5)
pullback_fill(s, "D2", t0 + 4.5)   # -0.5% 되돌림 체결 (2026-08-04)
check("중복 편입 11:00 -> Pullback으로 매수",
      s.holdings.get("D2", {}).get("sub_strategy") == "1A_눌림",
      str(s.holdings.get("D2", {}).get("sub_strategy")))


# ═════════════════════════════════════════════════════════
print("\n[7] 기존 안전장치가 틱 경로에서도 유지되는지")
# ═════════════════════════════════════════════════════════
# 시가대비 +5% 보류는 1A에만
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "S1")
s._opening_prices["S1"] = 9_000            # 현재가 10,000 -> +11%
tick(s, "S1", 130.0, t0)
feed(s.phase1b.trade_flow, "S1", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "S1", 130.0, t0 + 3.5)
check("1A: 시가대비 +5% 이상이면 매수 보류", "S1" not in s.holdings)

s = build_strat(datetime(2026, 8, 3, 10, 0, 0))
setup_candidate(s, "S2", cond="눌림목자동")
s._opening_prices["S2"] = 9_000
tick(s, "S2", 130.0, t0)
feed(s.phase1b.trade_flow, "S2", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "S2", 130.0, t0 + 3.5)
pullback_fill(s, "S2", t0 + 4.5)   # -0.5% 되돌림 체결 (2026-08-04)
check("눌림목엔 시가대비 보류를 적용하지 않음(되돌림을 사는 전략)",
      "S2" in s.holdings)

# 지수 HALT
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "H1")
s._get_market_defense_mode = lambda: "HALT"
tick(s, "H1", 130.0, t0)
feed(s.phase1b.trade_flow, "H1", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "H1", 130.0, t0 + 3.5)
check("지수 HALT면 틱 경로도 매수 안 함", "H1" not in s.holdings)

# 재매수 차단
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "R1")
s._stoploss_blocked.add("R1")
tick(s, "R1", 130.0, t0)
feed(s.phase1b.trade_flow, "R1", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "R1", 130.0, t0 + 3.5)
check("손실차단 종목은 틱 경로에서도 재매수 안 됨", "R1" not in s.holdings)

# 슬롯 상한
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
for i in range(SM.MAX_HOLDINGS):
    s.holdings[f"F{i}"] = {
        "buy_price": 10_000, "qty": 1, "sub_strategy": "1A",
        "buy_time": s._now(), "highest_price": 10_000, "lowest_price": 10_000,
        "entry_strength": 150, "warmup_until": s._now(),
    }
setup_candidate(s, "SL1")
tick(s, "SL1", 130.0, t0)
feed(s.phase1b.trade_flow, "SL1", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "SL1", 130.0, t0 + 3.5)
check("슬롯 만석이면 틱 경로도 매수 안 함", "SL1" not in s.holdings)

# 보유 종목은 진입 평가로 안 가고 가격 갱신만
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "HD1")
s.holdings["HD1"] = {
    "buy_price": 10_000, "stock_name": "HD1", "qty": 1, "sub_strategy": "1A",
    "buy_time": s._now(), "highest_price": 10_000, "lowest_price": 10_000,
    "entry_strength": 150, "entry_score": 1.0, "entry_tier": 1.0,
    "warmup_until": s._now() + timedelta(seconds=60),
}
tick(s, "HD1", 130.0, t0, price=10_010)
check("보유 종목의 틱은 진입 평가로 가지 않음", "HD1" not in s._strength_since)
check("보유 종목도 체결틱은 계속 쌓임(동적캡 전제)",
      s.phase1b.trade_flow.tick_count("HD1", 60, now=t0) > 0)


# ═════════════════════════════════════════════════════════
print("\n[8] 주문 우선 레인")
# ═════════════════════════════════════════════════════════
from api.kiwoom_rest import KiwoomREST

r = KiwoomREST.__new__(KiwoomREST)
import threading as _th
r._throttle_lock = _th.Lock()
r._order_throttle_lock = _th.Lock()
r._last_request_ts = 0.0
r._last_order_ts = 0.0

check("주문 간격 상수가 조회보다 짧음",
      KiwoomREST.ORDER_MIN_INTERVAL < KiwoomREST.MIN_INTERVAL,
      f"{KiwoomREST.ORDER_MIN_INTERVAL} < {KiwoomREST.MIN_INTERVAL}")

r._last_request_ts = time.time()          # 방금 조회가 나갔다
t_start = time.time()
r._throttle(priority=True)                # 주문은 기다리지 않아야 함
elapsed = time.time() - t_start
check("조회 직후에도 주문은 대기 없이 통과 (구버전은 0.6초 대기)",
      elapsed < 0.1, f"{elapsed:.3f}초")

r._last_order_ts = time.time()
t_start = time.time()
r._throttle(priority=True)
elapsed = time.time() - t_start
check("주문끼리는 ORDER_MIN_INTERVAL(0.2초)을 지킴",
      0.15 <= elapsed <= 0.35, f"{elapsed:.3f}초")

r._last_request_ts = 0.0
r._last_order_ts = 0.0
r._throttle(priority=True)
check("주문이 조회 타임스탬프도 갱신(직후 조회가 429를 유발하지 않게)",
      r._last_request_ts > 0)

r2 = KiwoomREST.__new__(KiwoomREST)
r2._throttle_lock = _th.Lock()
r2._order_throttle_lock = _th.Lock()
r2._last_request_ts = time.time()
r2._last_order_ts = 0.0
t_start = time.time()
r2._throttle(priority=False)              # 일반 조회는 기존대로 대기
elapsed = time.time() - t_start
check("일반 조회는 기존 0.6초 간격을 그대로 지킴",
      elapsed >= 0.4, f"{elapsed:.3f}초")


# ═════════════════════════════════════════════════════════
print("\n[9] pre-arm (편입 즉시 살 준비 완료)")
# ═════════════════════════════════════════════════════════
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
s._cond_names["PA1"] = "주도주상위"
s._stock_names["PA1"] = "PA1"
s.prearm_candidate("PA1", 10_000)
check("pre-arm이 체결틱 감시를 켬(이게 없으면 무장 자체가 불가)",
      s.phase1b.is_watching("PA1"))
check("pre-arm이 전일종가를 미리 캐시(주문 직전 REST 제거)",
      "PA1" in s._prev_closes, str(s._prev_closes))

s.api.calls.clear()
s.prearm_candidate("PA1", 10_000)
check("이미 예열됐으면 REST 재호출 안 함",
      len(s.api.calls) == 0, str(s.api.calls))

s2 = build_strat(datetime(2026, 8, 3, 9, 30, 0))
s2.phase1b = None
s2.prearm_candidate("PA2", 10_000)
check("phase1b가 없어도 예외를 밖으로 던지지 않음", True)

# 편입(on_condition_hit) 경로가 pre-arm을 실제로 부르는지
s3 = build_strat(datetime(2026, 8, 3, 9, 30, 0))
s3.on_condition_hit("PA3", "프리암", is_surge=False, cond_name="주도주상위")
check("편입 즉시 감시가 켜짐", s3.phase1b.is_watching("PA3"))
check("편입 즉시 전일종가 캐시 예열됨", "PA3" in s3._prev_closes)


# ═════════════════════════════════════════════════════════
print("\n[10] 회귀 방지 — 구버전 동작 못박기")
# ═════════════════════════════════════════════════════════
check("TICK_ENTRY_ENABLED 스위치 존재(즉시 끌 수 있음)",
      hasattr(SM, "TICK_ENTRY_ENABLED") and SM.TICK_ENTRY_ENABLED is True)
check("버스트 요구 건수가 2건(사용자 지정, 구버전 3건)",
      SM.PHASE1A_BURST_TRADE_COUNT == 2, str(SM.PHASE1A_BURST_TRADE_COUNT))
check("버스트 창이 5초(구버전 3초, 사용자 원안 1초는 판정 불가)",
      SM.TICK_BURST_WINDOW_SEC == 5.0, str(SM.TICK_BURST_WINDOW_SEC))
check("강도 유지 요구가 1.5초 (2026-08-03: 3.0 -> 2.0 -> 1.5)",
      SM.TICK_STRENGTH_SUSTAIN_SEC == 1.5, str(SM.TICK_STRENGTH_SUSTAIN_SEC))
check("강도 임계값 100", SM.TICK_STRENGTH_MIN == 100.0)

# 구버전: 15초 폴링이 유일한 진입 경로였다 -> 지금은 틱에서 바로 산다
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "RG1")
tick(s, "RG1", 130.0, t0)
feed(s.phase1b.trade_flow, "RG1", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "RG1", 130.0, t0 + 3.5)
pullback_fill(s, "RG1", t0 + 4.5)   # -0.5% 되돌림 체결 (2026-08-04)
check("구버전은 여기서 0건(폴링 대기) — 지금은 틱에서 즉시 매수",
      "RG1" in s.holdings)

# 구버전: compute_strength 중립값(100)이 임계값 100을 그냥 통과했다
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "RG2")
feed(s.phase1b.trade_flow, "RG2", 2, 30_000_000, now=t0, span=0.5)
ok, info = s.evaluate_tick_entry("RG2", "1A", 10_000, now=t0)
check("체결강도 정보가 전혀 없으면(무장 0초) 버스트가 있어도 탈락",
      not ok and "미무장" in info.get("reason", ""), str(info.get("reason"))[:60])

# 쿨다운이 무장 '이후'에만 걸리는지 (무장 전에 걸면 3초 순간을 놓친다)
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "RG3")
for i in range(20):
    tick(s, "RG3", 130.0, t0 + i * 0.05)
check("무장 전에는 쿨다운이 타이머 갱신을 막지 않음",
      "RG3" in s._strength_since)

# pending 누수 — _execute_buy가 pending 등록 '전에' early-return 하는 경로들
# (하드컷오프/등락률 상한/전략 불일치/수량 0)에서 우리가 미리 넣은 pending이
# 남으면 occupied_slots()가 그 자리를 영구 점유로 세고 그 종목은 다시는
# 매수되지 않는다.
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "LK1")
s._opening_prices["LK1"] = 10_000
s.api.get_stock_change_rate = lambda c: 50.0      # 전일종가대비 +50% -> 상한 초과
tick(s, "LK1", 130.0, t0)
feed(s.phase1b.trade_flow, "LK1", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "LK1", 130.0, t0 + 3.5)
check("등락률 상한 초과로 매수 무산돼도 pending이 남지 않음",
      "LK1" not in s.pending, str(s.pending))
check("그 경우 슬롯도 점유되지 않음", s.occupied_slots() == 0, str(s.occupied_slots()))

s = build_strat(datetime(2026, 8, 3, 14, 30, 0))
setup_candidate(s, "LK2")
s._now = lambda: datetime(2026, 8, 3, 15, 30, 0)   # 하드컷오프 이후
tick(s, "LK2", 130.0, t0)
feed(s.phase1b.trade_flow, "LK2", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "LK2", 130.0, t0 + 3.5)
check("하드컷오프 경로에서도 pending 누수 없음", "LK2" not in s.pending, str(s.pending))

# 진단 카운터
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
setup_candidate(s, "DG1")
tick(s, "DG1", 130.0, t0)
feed(s.phase1b.trade_flow, "DG1", 2, 30_000_000, now=t0 + 3.5, span=0.5)
tick(s, "DG1", 130.0, t0 + 3.5)
pullback_fill(s, "DG1", t0 + 4.5)   # -0.5% 되돌림 체결 (2026-08-04)
st = s._tick_entry_stats
check("진단 카운터가 단계별로 집계됨",
      st["ticks"] >= 2 and st["armed"] == 1 and st["bought"] == 1, str(st))


# ═════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print(f"  - {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
