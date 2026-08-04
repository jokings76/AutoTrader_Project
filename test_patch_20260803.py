"""2026-08-03 패치 격리 검증 — 익절(청산) 로직 수술.

08-03 첫 실거래에서 드러난 결함을 못박는다. 셋 다 파라미터 튜닝이 아니라
**로직 결함**이었고, 오늘 청산 16건 중 익절 계열이 사실상 본전만 낸 원인이다.

  결함 ① `cap_exit = cap >= TP_CAP_UPGRADED` 인데 1A 기본캡(0.025)과
        TP_CAP_UPGRADED(0.025)가 같은 값이라 **모든 1A가 매수 직후부터**
        '동적캡 즉시매도' 대상이었다 (실측 6건이 75~250초 만에 청산).
  결함 ② `entry_strength`가 진입 순간(=버스트가 터지는 순간)의 강도라
        거의 항상 국소 최고점이었다. 08-03 실거래에서 3건이 정확히 300
        (compute_strength 상한)으로 포화. 그 값을 기준으로 `현재 < 기준x0.8`을
        재면 **정상으로 돌아오기만 해도 '하락'**이 된다(구조적 필연).
  결함 ③ '익절' 로직인 동적캡 즉시매도가 손실 구간에서도 발동해
        -0.99% / -0.48% / -0.43%를 실현시켰다.

  설계 변경 '익절 조기확정' -> '본전스톱'. 08-03 조기확정 2건이 모두 매도 후
        더 올랐다(037070 매도가 대비 +12.0%, 439960 +3.4%). 같은 지점에서
        팔지 않고 손절선을 본전으로 올려 하방을 닫고 상단을 캡까지 연다.

네트워크·DB·키움 API를 타지 않는 순수 격리 테스트.
실행: python test_patch_20260803.py   (종료코드 0 = 전원 통과)
"""
import sys
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


# ─── 스텁 (test_patch_20260801/02와 동일 계약) ──────────────────
class _Repo:
    rows, sells = [], []
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
    def get_minute_candles(self, code, interval=1, count=1, base_date=None):
        # volume_ratio가 '거래량 하락'(<1.0)으로 판정되도록: 최신봉 거래량이 작다
        return [{"time_str": "20260803140000", "open": 10_000, "high": 10_050,
                 "low": 9_950, "close": 10_000, "volume": 10 if i == 0 else 1000}
                for i in range(count)]
    def get_orderable_amount(self): return 10_000_000
    def get_stock_change_rate(self, code): return 3.0
    def get_index_change_rate(self, s="001"): return 0.0
    def get_current_price(self, code): return 10_000


class _OrderMgr:
    def __init__(self): self.orders = []
    def buy(self, code, qty, **kw): return {"success": True, "ord_no": "1"}
    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"code": code, "qty": qty})
        return {"success": True, "ord_no": "2", "price": price}
    def get_stock_name(self, code): return code


NOW = datetime(2026, 8, 3, 10, 30, 0)


def build(now_dt=NOW):
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows, _Repo.sells = [], []
    return SM.StrategyManager(
        kiwoom_rest=_Rest(), order_manager=_OrderMgr(),
        phase1b_controller=Phase1BController(), portfolio_optimizer=None,
        now_func=lambda: now_dt,
    )


def put_pos(strat, code="000001", buy_price=10_000, sub="1A",
            warmup_done=True, entry_strength=300.0, now_dt=NOW):
    """보유 포지션 1개를 심는다. 워밍업은 기본적으로 끝난 상태."""
    strat.holdings[code] = {
        # trade_id가 있어야 _execute_sell이 update_sell로 사유를 남긴다
        # (없으면 "trade_id 없음 -> DB 갱신 스킵" 경로로 빠져 검증이 불가능).
        "trade_id": 1,
        "qty": 100, "buy_price": buy_price, "buy_time": now_dt - timedelta(minutes=5),
        "stock_name": code, "sub_strategy": sub,
        "warmup_until": (now_dt - timedelta(seconds=1)) if warmup_done
                        else (now_dt + timedelta(seconds=60)),
        "entry_strength": entry_strength, "highest_price": buy_price,
        "lowest_price": buy_price,
    }
    return strat.holdings[code]


def sold(strat, code="000001"):
    return code not in strat.holdings


# ═════════════════════════════════════════════════════════
print("=" * 62)
print("2026-08-03 익절 로직 수술 검증")
print("=" * 62)

print("\n[1] 캡 상향 — 조기확정이 만들던 '수수료 내고 본전'에서 탈출")
check("1A 기본캡 4.0% (구 2.5%)", abs(SM.TAKE_PROFIT_CAP - 0.040) < 1e-9,
      f"{SM.TAKE_PROFIT_CAP}")
check("눌림 캡 2.5% (구 1.5%)", abs(SM.TAKE_PROFIT_CAP_PULLBACK - 0.025) < 1e-9,
      f"{SM.TAKE_PROFIT_CAP_PULLBACK}")
check("개장초반 캡 2.5% (구 1.5%)", abs(SM.TAKE_PROFIT_CAP_EARLY - 0.025) < 1e-9,
      f"{SM.TAKE_PROFIT_CAP_EARLY}")
check("상향 목표캡 6.0% (구 2.5%)", abs(SM.TP_CAP_UPGRADED_MAX - 0.060) < 1e-9,
      f"{SM.TP_CAP_UPGRADED_MAX}")
# 결함 ①의 뿌리: 기본캡과 상향캡이 같은 값이면 안 된다.
check("기본캡 != 상향캡 (같으면 결함 ① 재발)",
      abs(SM.TAKE_PROFIT_CAP - SM.TP_CAP_UPGRADED_MAX) > 1e-9)

print("\n[2] 본전스톱 — 현재 ON (2026-08-04 재활성화)")
# 도입 당일 저녁엔 재생 결과(무장 게이트 없는 약식 시뮬레이션)가 나빠서 껐었다.
# 08-04 오전 실거래 대조 분석(같은 09:00~09:40 창) 결과 조기확정 폐지로
# 비워진 중간 안전판을 아무것도 대신하지 못해 순손익 +165,345원 -> -427,200원,
# 손절 0건 -> 5건으로 악화 — 사용자 지정으로 재활성화.
# 이 섹션은 (a) 기본값이 ON인지 (b) **껐을 때도 로직이 여전히 정상인지**를
# 둘 다 확인한다. 로직이 썩으면 되살릴 때 조용히 깨지기 때문이다.
check("본전스톱 기본값 ON", SM.BREAKEVEN_STOP_ENABLED is True)
check("무장 지점 상수는 보존(순 +1.0%)", abs(SM.BREAKEVEN_TRIGGER - 0.010) < 1e-9)
check("바닥 상수도 보존(본전 0%)", abs(SM.BREAKEVEN_FLOOR - 0.0) < 1e-9)

px_arm = int(10_000 * (1 + SM.BREAKEVEN_TRIGGER + SM.ROUND_TRIP_COST)) + 1

# --- ON 상태(기본값): +1% 찍고 되돌리면 본전스톱으로 청산 ---
s = build()
pos = put_pos(s)
s.on_price_update("000001", px_arm)
check("ON이면 순+1% 도달 시 무장", pos.get("breakeven_armed") is True)
s.on_price_update("000001", 10_000)
check("ON이면 본전 이탈 시 청산", sold(s))
check("청산 사유가 본전스톱",
      any("본전스톱" in (r.get("exit_reason") or "") for r in _Repo.sells),
      str([r.get("exit_reason") for r in _Repo.sells])[:70])

# 구버전 '익절 조기확정' 문자열은 플래그와 무관하게 코드에서 완전히 제거돼야 한다.
check("조기확정 문자열이 코드에서 제거됨",
      "익절 조기확정" not in open(
          "core/strategy_manager.py", encoding="utf-8").read().split(
              "# (2026-08-03) rising is False")[0].split("exit_reason = (")[-1])

# --- OFF로 껐을 때도 로직이 살아있는지 (플래그만 임시 전환) ---
SM.BREAKEVEN_STOP_ENABLED = False
try:
    s2 = build()
    p2 = put_pos(s2, code="000002")
    s2.on_price_update("000002", px_arm)
    check("[OFF] 무장 자체를 하지 않음", not p2.get("breakeven_armed"))
    s2.on_price_update("000002", 10_000)
    check("[OFF] 본전 이탈에도 청산 안 함", not sold(s2, "000002"))
    # 무장 전에는 발동하지 않는다(진입 직후 흔들림에 잘리면 안 됨) — ON 상태로 확인
    SM.BREAKEVEN_STOP_ENABLED = True
    s3 = build()
    put_pos(s3, code="000003b")
    s3.on_price_update("000003b", 9_990)
    check("[ON] 무장 전에는 본전스톱 미발동", not sold(s3, "000003b"))
finally:
    SM.BREAKEVEN_STOP_ENABLED = True    # 원상복구 — 실제 기본값(ON)으로

print("\n[3] 결함 ① — 동적캡 즉시매도가 모든 1A에 걸리던 문제")
s3 = build()
p3 = put_pos(s3, code="000003", sub="1A")
cap, _ = s3._take_profit_cap(p3)
check("1A 기본 포지션은 아직 '상향' 상태가 아님",
      not p3.get("tp_cap_upgraded"))
# 구버전 조건 재현: cap >= TP_CAP_UPGRADED(0.025) 였다면 1A(0.025)는 항상 True
check("구버전 조건이었다면 1A가 항상 대상이 됐음(회귀 근거)",
      SM.TP_CAP_UPGRADED <= 0.025)
check("새 조건은 tp_cap_upgraded 플래그 기반",
      "cap_exit = bool(pos.get(\"tp_cap_upgraded\"))" in open(
          "core/strategy_manager.py", encoding="utf-8").read())

print("\n[4] 결함 ② — entry_strength(진입 스파이크) 대신 기준선 리앵커")
s4 = build()
p4 = put_pos(s4, code="000004", entry_strength=300.0)  # 상한 포화값
check("기준선은 처음엔 비어 있음", s4._strength_baseline(p4) == 0.0)
# 강도 데이터가 없으면(중립값) 기준선을 잡지 않는다 -> 판단 보류
s4._maybe_anchor_strength_baseline(p4, "000004")
check("중립값(데이터 없음)은 기준선으로 삼지 않음",
      s4._strength_baseline(p4) == 0.0)
check("기준선 없으면 상승 판정도 보류(None)",
      s4._is_strength_rising_vs_entry(p4, "000004") is None)
# 실제 틱이 쌓이면 그 값으로 기준선을 잡는다
import time as _t
_now = _t.time()
for i in range(12):
    s4.phase1b.trade_flow.add_tick("000004", 10_000, "buy", 10, now=_now - i * 0.2)
    s4.phase1b.trade_flow.add_tick("000004", 10_000, "sell", 5, now=_now - i * 0.2)
s4._maybe_anchor_strength_baseline(p4, "000004")
base = s4._strength_baseline(p4)
check("워밍업 후 실제 강도로 기준선 고정", base > 0 and base != 300.0, f"{base:.0f}")
check("진입 스파이크(300)를 기준으로 쓰지 않음", base != p4["entry_strength"])

print("\n[5] 결함 ③ — '익절' 로직이 손실 구간에서 발동하던 문제")
src = open("core/strategy_manager.py", encoding="utf-8").read()
check("cap_exit 경로에 순이익 가드 존재",
      "if cap_exit and not loss_rebound:" in src and
      "if self._net_rate(pos[\"buy_price\"], price) <= 0:" in src)

s5 = build()
p5 = put_pos(s5, code="000005", sub="1A")
p5["tp_cap"] = SM.TP_CAP_UPGRADED_MAX
p5["tp_cap_upgraded"] = True          # 상향된 포지션 = cap_exit 대상
p5["strength_baseline"] = 200.0       # 기준선 확보
for i in range(12):                   # 강도 하락 상황 만들기(매도 우위)
    s5.phase1b.trade_flow.add_tick("000005", 9_800, "sell", 50, now=_now - i * 0.2)
    s5.phase1b.trade_flow.add_tick("000005", 9_800, "buy", 1, now=_now - i * 0.2)
s5._update_dynamic_caps()
check("손실 구간에서는 동적캡 즉시매도 안 함", not sold(s5, "000005"))

print("\n[6] 청산 우선순위 — 손절이 본전스톱보다 먼저")
# 본전스톱이 켜져 있어도(현재 기본값) 손절이 우선이어야 한다(순서가 뒤집히면
# -4% 급락을 '본전스톱'으로 기록해 사후 분석이 어긋난다).
SM.BREAKEVEN_STOP_ENABLED = True
try:
    s6 = build()
    p6 = put_pos(s6, code="000006")
    s6.on_price_update("000006", px_arm)          # 먼저 무장
    check("무장 확인", p6.get("breakeven_armed") is True)
    s6.on_price_update("000006", 9_600)           # -4% 급락
    check("급락 시 손절로 청산", sold(s6, "000006"))
finally:
    SM.BREAKEVEN_STOP_ENABLED = True   # 원상복구 — 실제 기본값(ON)으로
check("사유가 손절(본전스톱 아님)",
      any("손절" in (r.get("exit_reason") or "") for r in _Repo.sells),
      str([r.get("exit_reason") for r in _Repo.sells])[:70])

print("\n[7] 캡 도달 시 정상 익절 (상단이 열렸는지)")
s7 = build()
put_pos(s7, code="000007", sub="1A")
# 구버전 캡(2.5%)에서는 팔렸어야 하고, 새 캡(4.0%)에서는 아직 보유
px_25 = int(10_000 * (1 + 0.025 + SM.ROUND_TRIP_COST)) + 1
s7.on_price_update("000007", px_25)
check("순 +2.5%에서는 아직 안 팜 (구버전이면 여기서 익절)", not sold(s7, "000007"))
px_40 = int(10_000 * (1 + SM.TAKE_PROFIT_CAP + SM.ROUND_TRIP_COST)) + 2
s7.on_price_update("000007", px_40)
check("순 +4.0% 도달 시 익절 캡 청산", sold(s7, "000007"))

print("\n[9] 손절 — 워밍업 중에도 반드시 작동해야 한다")
# 08-03 발견: on_price_update의 워밍업 return이 손절 판정보다 위에 있어서
# 매수 직후 60초는 얼마가 빠지든 무방비였다. 가격 폴백 태스크도 결국 이
# 함수를 부르므로 우회 경로도 없었다.
s9 = build()
p9 = put_pos(s9, code="000009", warmup_done=False)   # 워밍업 진행 중
check("워밍업 중인 포지션인지 확인", s9._now() < p9["warmup_until"])
s9.on_price_update("000009", 9_600)                  # -4% 급락
check("워밍업 중에도 손절 발동", sold(s9, "000009"))
check("사유가 손절",
      any("손절" in (r.get("exit_reason") or "") for r in _Repo.sells),
      str([r.get("exit_reason") for r in _Repo.sells])[:60])

# 워밍업 중 '손절이 아닌' 판정은 여전히 보류되어야 한다(성급한 청산 방지).
s9b = build()
p9b = put_pos(s9b, code="000010", warmup_done=False)
s9b.on_price_update("000010", px_arm)                # 순 +1% (본전스톱 무장 지점)
check("워밍업 중엔 본전스톱 무장 안 함(강도류 판단 보류 유지)",
      not p9b.get("breakeven_armed") and not sold(s9b, "000010"))

# 손절선 자체는 그대로
check("손절선 -3% 유지", abs(SM.STOP_LOSS_RATE - (-0.03)) < 1e-9, f"{SM.STOP_LOSS_RATE}")

print("\n[10] 시간대 계수 — 점심 완화 제거 (08-03 실측: 완화 구간 전부 손실)")
mult = dict(SM.TICK_BURST_TIME_MULT)
check("점심(11:30~) 계수 1.00 (구 0.65)", abs(mult[(11, 30)] - 1.00) < 1e-9,
      f"{mult[(11, 30)]}")
check("개장 구간은 그대로 1.00", abs(mult[(9, 0)] - 1.00) < 1e-9)
check("계수가 1.00을 넘지 않음(강화가 아니라 '완화 제거')",
      all(v <= 1.0 for _, v in SM.TICK_BURST_TIME_MULT))
s10 = build(datetime(2026, 8, 3, 12, 0, 0))
check("12:00 시점 계수가 1.00으로 조회됨",
      abs(s10.burst_time_multiplier(datetime(2026, 8, 3, 12, 0, 0)) - 1.00) < 1e-9)
check("09:30 시점도 1.00",
      abs(s10.burst_time_multiplier(datetime(2026, 8, 3, 9, 30, 0)) - 1.00) < 1e-9)

print("\n[11] 자동종료 — 할 일 끝나면 즉시 (종가베팅만으론 안 됨)")
msrc = open("main.py", encoding="utf-8").read()
for f in ("_closing_bet_done", "_force_close_done", "_backtest_done"):
    check(f"완료 플래그 {f} 존재", f"self.{f}" in msrc)
check("셋 다 완료여야 종료(AND 조건)",
      "self._closing_bet_done" in msrc and "and self._force_close_done" in msrc
      and "and self._backtest_done" in msrc)
check("15:40 하드 폴백 유지", 'target_time = "15:40"' in msrc)
check("강제청산 완료 판정이 트리거 블록 밖(매 루프 재평가)",
      "if triggered and not self.strategy_mgr.holdings:" in msrc)
check("보유가 남으면 완료로 치지 않음(오버나이트 방지)",
      "not self.strategy_mgr.holdings" in msrc)
check("종가베팅/백테스트는 실패해도 완료 처리(무한 대기 방지)",
      msrc.count("finally:") >= 2)

print("\n[12] 종목명 추출 — 실시간 편입 push의 'name'은 종목명이 아니다")
# 08-03 실거래: 매수 알림·로그·holdings의 종목명이 전부 "조건검색"으로 찍혔다.
# 원인은 실시간 편입 push(type='02') 최상위 'name'이 **실시간 타입 라벨**인데
# 후보 키에 들어 있어서, "찾았다"고 판단해 REST 폴백이 무력화된 것.
import main as M

RT_PUSH = {  # 키움 실시간 편입 push 실제 형태 (api/kiwoom_ws.py:550 실측 기록)
    "type": "02", "name": "조건검색", "item": "079650",
    "values": {"841": "3", "9001": "079650", "843": "I", "20": "100621"},
}
got = M._extract_stock_name(RT_PUSH, "079650")
check("실시간 push에서 '조건검색'을 종목명으로 쓰지 않음", got != "조건검색", got)
check("종목명이 없으면 stock_code 반환(=REST 폴백 신호)", got == "079650", got)

# 기동 스냅샷(CNSRREQ)은 '302'에 진짜 이름이 있다 — 이건 계속 살아야 한다.
SNAPSHOT = {"9001": "A002990", "302": "금호건설", "10": "5000"}
check("스냅샷의 302 종목명은 정상 추출",
      M._extract_stock_name(SNAPSHOT, "002990") == "금호건설")
# 다른 후보 키도 유지되는지(회귀 방지)
check("hng_name 폴백 유지",
      M._extract_stock_name({"hng_name": "삼성전자"}, "005930") == "삼성전자")
check("dict가 아니면 stock_code", M._extract_stock_name(None, "005930") == "005930")
check("빈 문자열은 이름으로 안 봄",
      M._extract_stock_name({"302": "   "}, "005930") == "005930")
# 'name' 키가 후보 목록에서 실제로 빠졌는지 (구버전 회귀 방지)
check("'name'이 후보 키에서 제거됨",
      M._extract_stock_name({"name": "아무거나"}, "005930") == "005930")

print("\n[13] 지수 하락 가드 (-3%, 11:00~) — 2026-08-03 신규")


def guard_strat(hh, mm, kospi=0.0, kosdaq=0.0):
    st = build(datetime(2026, 8, 3, hh, mm, 0))
    st._kospi_rate, st._kosdaq_rate = kospi, kosdaq
    st._market_rate_at = st._now()          # 캐시 신선 -> REST 재조회 안 함
    return st


check("가드 활성 상수", SM.INDEX_GUARD_ENABLED is True)
check("임계 -5% (2026-08-03: -3.0에서 변경)",
      abs(SM.INDEX_GUARD_THRESHOLD - (-5.0)) < 1e-9, str(SM.INDEX_GUARD_THRESHOLD))
check("감시 시작 11:00", SM.INDEX_GUARD_FROM == SM.time(11, 0))
check("본전청산 마감 11:30", SM.INDEX_GUARD_BREAKEVEN_UNTIL == SM.time(11, 30))
check("강제청산 14:50", SM.INDEX_GUARD_FORCE_CLOSE == SM.time(14, 50))

# --- 발동 조건 ---
check("11:00 전에는 -6%여도 발동 안 함(개장 급락 과민반응 방지)",
      not guard_strat(10, 59, -6.0, -6.0)._is_index_guard_active())
check("11:00 이후 코스피만 -5%여도 발동(둘 중 하나)",
      guard_strat(11, 0, -5.1, +0.5)._is_index_guard_active())
check("11:00 이후 코스닥만 -5%여도 발동",
      guard_strat(11, 0, +0.5, -5.2)._is_index_guard_active())
check("-4.9%면 발동 안 함(경계)",
      not guard_strat(11, 5, -4.9, -4.9)._is_index_guard_active())
check("지수 데이터가 0.0(미조회)이면 발동 안 함(보수적)",
      not guard_strat(12, 0, 0.0, 0.0)._is_index_guard_active())

# --- 신규매수 차단 ---
sG = guard_strat(11, 10, -5.5, -1.0)
check("가드 발동 중 can_buy_more=False", not sG.can_buy_more())
sG2 = guard_strat(11, 10, -2.0, -1.0)
check("가드 미발동이면 can_buy_more 정상", sG2.can_buy_more())

# --- 1단계: 11:30 이전, 본전 이상이면 청산 ---
sB = guard_strat(11, 10, -5.5, -1.0)
put_pos(sB, code="G001", buy_price=10_000, now_dt=sB._now())
# 순 0% = 가격 +0.23%(수수료). 그보다 위면 청산 대상.
px_be = int(10_000 * (1 + SM.ROUND_TRIP_COST)) + 2
sB.on_price_update("G001", px_be)
check("11:10 본전 이상 -> 지수 가드 청산", sold(sB, "G001"))
check("청산 사유가 '지수 가드 본전청산'",
      any("지수 가드 본전청산" in (r.get("exit_reason") or "") for r in _Repo.sells),
      str([r.get("exit_reason") for r in _Repo.sells])[:60])

# 손실 중이면 아직 안 판다(저점 매도 방지)
sL = guard_strat(11, 10, -5.5, -1.0)
put_pos(sL, code="G002", buy_price=10_000, now_dt=sL._now())
sL.on_price_update("G002", 9_900)     # 순 -1.2%
check("11:10 손실 중이면 청산 안 함(회복 기회)", not sold(sL, "G002"))

# --- 2단계: 11:30~14:50 사이엔 보유 유지, 14:50에 강제청산 ---
sM = guard_strat(13, 0, -5.5, -1.0)
put_pos(sM, code="G003", buy_price=10_000, now_dt=sM._now())
sM.on_price_update("G003", 9_900)
check("13:00 손실분은 아직 보유(14:50까지 회복 대기)", not sold(sM, "G003"))

sF = guard_strat(14, 50, -5.5, -1.0)
put_pos(sF, code="G004", buy_price=10_000, now_dt=sF._now())
sF.on_price_update("G004", 9_900)
check("14:50 강제청산 발동", sold(sF, "G004"))
check("사유가 '지수 가드 강제청산'",
      any("지수 가드 강제청산" in (r.get("exit_reason") or "") for r in _Repo.sells),
      str([r.get("exit_reason") for r in _Repo.sells])[:60])

# --- 우선순위: 손절이 가드보다 먼저 ---
sP = guard_strat(11, 10, -5.5, -1.0)
put_pos(sP, code="G005", buy_price=10_000, now_dt=sP._now())
sP.on_price_update("G005", 9_600)     # -4%
check("가드 발동 중에도 급락은 손절로 청산", sold(sP, "G005"))
check("사유가 손절(가드 아님)",
      any("손절" in (r.get("exit_reason") or "") for r in _Repo.sells),
      str([r.get("exit_reason") for r in _Repo.sells])[:60])

# --- 가드 미발동 시 기존 동작 불변 (회귀 방지) ---
sN = guard_strat(11, 10, -2.0, -1.0)
put_pos(sN, code="G006", buy_price=10_000, now_dt=sN._now())
sN.on_price_update("G006", px_be)
check("가드 미발동이면 본전 근처에서 팔지 않음(기존 동작 유지)",
      not sold(sN, "G006"))

# --- 진단 분류 (기타로 뭉개지지 않는지) ---
check("가드 탈락 사유가 분류됨",
      SM.StrategyManager._reject_category("지수 하락 가드(-3%) — 신규매수 중단")
      != "기타")

print("\n[15] 시간정리/정체정리 — '슬롯 만석'일 때만 (2026-08-03 변경)")
# 존재 이유가 '슬롯 기회비용'인데 슬롯이 남으면 비울 이유가 없다.
# 08-03 실측: 동시보유 최대 2/6이라 자리가 하루 종일 남았는데도 4건이 나갔다.


def full_strat(hh, mm, n_hold, kospi=0.0, kosdaq=0.0, held_min=45):
    st = build(datetime(2026, 8, 3, hh, mm, 0))
    st._kospi_rate, st._kosdaq_rate = kospi, kosdaq
    st._market_rate_at = st._now()
    for i in range(n_hold):
        st.holdings[f"H{i}"] = {
            "trade_id": 1, "qty": 10, "buy_price": 10_000,
            "buy_time": st._now() - timedelta(minutes=held_min),
            "stock_name": f"H{i}", "sub_strategy": "1A", "warmup_until": None,
            "entry_strength": 150.0, "highest_price": 10_000, "lowest_price": 10_000,
        }
    return st


def netpx(p):
    return int(10_000 * (1 + p / 100 + SM.ROUND_TRIP_COST))


# 슬롯 여유 -> 시간정리 안 함 (구버전은 여기서 팔았다)
sF = full_strat(11, 10, 1)
sF.on_price_update("H0", netpx(-1.0))
check("슬롯 여유(1/6)면 45분 보유해도 시간정리 안 함", "H0" in sF.holdings)
sF.check_timeouts()
check("check_timeouts 경로도 슬롯 여유면 안 팜(두 번째 경로)", "H0" in sF.holdings)

# 슬롯 만석 -> 기존대로 시간정리
sFull = full_strat(11, 10, SM.MAX_HOLDINGS)
sFull.on_price_update("H0", netpx(-1.0))
check("슬롯 만석이면 시간정리 발동(기존 동작 유지)", "H0" not in sFull.holdings)
check("사유에 '슬롯 만석' 표기",
      any("슬롯 만석" in (r.get("exit_reason") or "") for r in _Repo.sells),
      str([r.get("exit_reason") for r in _Repo.sells])[:60])

# 정체정리도 동일 규칙
sD = full_strat(11, 10, 1, held_min=SM.DEAD_POSITION_MIN + 1)
sD.on_price_update("H0", netpx(0.1))     # ±0.5% 밴드 안
check("슬롯 여유면 정체정리도 안 함", "H0" in sD.holdings)
sD2 = full_strat(11, 10, SM.MAX_HOLDINGS, held_min=SM.DEAD_POSITION_MIN + 1)
sD2.on_price_update("H0", netpx(0.1))
check("슬롯 만석이면 정체정리 발동", "H0" not in sD2.holdings)

print("\n[16] 지수 가드 -5% — 시간정리와의 충돌 해소 확인")
# 가드 사양("본전 이하는 손절선이나 14:50까지")이 30분 컷에 잘리면 무의미해진다.
check("가드 임계가 -5.0%", abs(SM.INDEX_GUARD_THRESHOLD - (-5.0)) < 1e-9)
check("[정합성] SEVERE_CRASH와 같은 임계 (한쪽만 고치면 어긋남)",
      abs(SM.INDEX_GUARD_THRESHOLD - SM.SEVERE_CRASH_THRESHOLD) < 1e-9,
      f"guard={SM.INDEX_GUARD_THRESHOLD} severe={SM.SEVERE_CRASH_THRESHOLD}")
check("[정합성] 두 규칙의 매수중단 시각도 동일",
      SM.INDEX_GUARD_FROM == SM.SEVERE_CRASH_ENTRY_CUTOFF)

sG1 = full_strat(11, 10, SM.MAX_HOLDINGS, kospi=-5.2, kosdaq=-1.0)
sG1.on_price_update("H0", netpx(-1.0))
sG1.check_timeouts()
check("가드 중 손실분은 슬롯 만석이어도 보유 유지(30분 컷 억제)",
      "H0" in sG1.holdings)

sG2 = full_strat(13, 0, SM.MAX_HOLDINGS, kospi=-5.2, kosdaq=-1.0)
sG2.on_price_update("H0", netpx(-1.0))
sG2.check_timeouts()
check("13:00에도 계속 보유(손절선/14:50까지)", "H0" in sG2.holdings)

sG3 = full_strat(13, 0, SM.MAX_HOLDINGS, kospi=-5.2, kosdaq=-1.0)
sG3.on_price_update("H0", netpx(-3.5))
check("가드 중에도 손절선 이탈은 손절", "H0" not in sG3.holdings)
check("사유가 손절",
      any("손절" in (r.get("exit_reason") or "") for r in _Repo.sells))

sG4 = full_strat(14, 50, SM.MAX_HOLDINGS, kospi=-5.2, kosdaq=-1.0)
sG4.on_price_update("H0", netpx(-1.0))
check("14:50 강제청산", "H0" not in sG4.holdings)

# 임계 미달이면 일반장과 동일
sG5 = full_strat(11, 10, SM.MAX_HOLDINGS, kospi=-4.0, kosdaq=-1.0)
sG5.on_price_update("H0", netpx(-1.0))
check("-4.0%(임계 미달)는 일반장과 동일하게 시간정리", "H0" not in sG5.holdings)

# 슬롯교체도 가드 중엔 멈춰야 한다(매도만 하고 재매수는 막히는 반쪽 동작 방지)
import core.slot_replacement as _SR
_sr_src = open("core/slot_replacement.py", encoding="utf-8").read()
check("슬롯교체에 지수 가드 차단이 있음",
      "_is_index_guard_active" in _sr_src)
sSR = full_strat(11, 10, SM.MAX_HOLDINGS, kospi=-5.2, kosdaq=-1.0)
check("가드 중 슬롯교체 시도가 즉시 반환(교체 0)",
      _SR.try_slot_replacement(sSR, None, 0, sSR._now()) == 0)

print("\n[17] 무장 1.5초")
check("무장 요구시간 1.5초", abs(SM.TICK_STRENGTH_SUSTAIN_SEC - 1.5) < 1e-9,
      str(SM.TICK_STRENGTH_SUSTAIN_SEC))
check("[불변] 쿨다운보다는 큼", SM.TICK_STRENGTH_SUSTAIN_SEC > SM.TICK_ENTRY_COOLDOWN_SEC)
check("[불변] 무장 TTL보다는 작음", SM.TICK_STRENGTH_SUSTAIN_SEC < SM.TICK_ARM_TTL_SEC)

print("\n[18] 슬롯 교체 — 대체후보가 '지금 무장 중'일 때만 (2026-08-03)")
# 08-03 실사례: 교체 3건이 전부 950160을 근거로 팔았는데 그 종목은 하루 종일
# 매수되지 않았다(무장 9회가 전부 11:06~11:23, 교체는 12:00·12:02·13:07).
# 235,860원을 실현손실로 확정하고 자리는 비워뒀다.
import time as _tm
import core.slot_replacement as _SR2

check("대체후보 무장 요구 플래그 ON", _SR2.REQUIRE_CANDIDATE_ARMED is True)


def armed_strat(arm_age_sec=None, with_ticks=True, score=99.0, code="CAND"):
    """대체후보 1개를 watch_list에 올리고 무장 상태를 원하는 대로 만든다."""
    st = build()
    now = _tm.time()
    st.watch_list_today.add(code)
    st._watch_scores[code] = score
    st.phase1b.start_watching(code)
    if with_ticks:
        for i in range(6):
            st.phase1b.trade_flow.add_tick(code, 10_000, "buy", 10, now=now - i * 0.3)
    if arm_age_sec is not None:
        st._armed_at[code] = now - arm_age_sec
        st._strength_since[code] = now - (SM.TICK_STRENGTH_SUSTAIN_SEC + 1)
    return st, now


# (a) 무장 기록이 아예 없는 후보 -> 자격 없음 (오늘 950160이 교체 시점에 이 상태)
st, _ = armed_strat(arm_age_sec=None)
check("무장 이력 없는 후보는 대체 자격 없음",
      _SR2.find_replacement_candidate(st, 1.0) is None)
check("is_armed_now도 False", not st.is_armed_now("CAND"))

# (b) 방금 무장 + 틱 살아있음 -> is_armed_now True
#     (선정까지 가려면 버스트도 필요 — 섹션 [19]에서 검증)
st, _n = armed_strat(arm_age_sec=5)
check("방금 무장한 후보는 is_armed_now=True", st.is_armed_now("CAND"))
for _i in range(2):   # 버스트까지 만들어야 최종 선정된다
    st.phase1b.trade_flow.add_tick("CAND", 10_000, "buy", 3_000, now=_n - _i * 0.3)
got = _SR2.find_replacement_candidate(st, 1.0)
check("무장+버스트면 후보로 선정됨", got is not None and got[0] == "CAND", str(got))

# (c) 무장했지만 TTL(120초) 경과 -> 자격 없음
st, _ = armed_strat(arm_age_sec=SM.TICK_ARM_TTL_SEC + 10)
check("무장 TTL 경과 후보는 자격 없음(이미 지나간 자리)",
      not st.is_armed_now("CAND"))
check("선정도 안 됨", _SR2.find_replacement_candidate(st, 1.0) is None)

# (d) ⚠️ 무장 기록은 있는데 틱이 끊긴 경우 -> 자격 없음
#     _armed_at은 그 종목의 '다음 틱'이 와야 만료 정리되므로, 틱이 끊기면
#     낡은 무장이 그대로 남는다(08-02 강도 타이머 잔류 사고와 같은 구조).
st, _ = armed_strat(arm_age_sec=5, with_ticks=False)
check("무장 기록이 있어도 최근 틱이 없으면 자격 없음(무장 잔재 차단)",
      not st.is_armed_now("CAND"))

# (e) 점수가 낮으면 무장했어도 자격 없음 (기존 점수 게이트 유지)
st, _ = armed_strat(arm_age_sec=5, score=0.1)
check("점수 미달이면 무장했어도 자격 없음",
      _SR2.find_replacement_candidate(st, 1.0) is None)

# (f) 재매수 차단 종목은 무장했어도 자격 없음 (기존 게이트 유지)
st, _ = armed_strat(arm_age_sec=5)
st._stoploss_blocked.add("CAND")
check("손실차단 종목은 무장했어도 자격 없음",
      _SR2.find_replacement_candidate(st, 1.0) is None)

# (g) 08-03 실사례 재현 — 무장이 40분 전이면 교체가 성립하지 않아야 한다
st, now = armed_strat(arm_age_sec=40 * 60)
check("[실사례] 40분 전 무장(950160 패턴)은 교체 자격 없음",
      _SR2.find_replacement_candidate(st, 1.0) is None)

# (h) 플래그를 끄면 옛 동작(점수만)으로 복귀하는지 — 롤백 경로 보장
_SR2.REQUIRE_CANDIDATE_ARMED = False
_SR2.REQUIRE_CANDIDATE_BURST = False
try:
    st, _ = armed_strat(arm_age_sec=None)
    got_old = _SR2.find_replacement_candidate(st, 1.0)
    check("[롤백] 두 플래그 OFF면 점수만으로 선정(구버전 동작)",
          got_old is not None and got_old[0] == "CAND", str(got_old))
finally:
    _SR2.REQUIRE_CANDIDATE_ARMED = True
    _SR2.REQUIRE_CANDIDATE_BURST = True

# (i) TICK_ENTRY_ENABLED가 꺼지면 무장 개념 자체가 없으므로 False
_prev = SM.TICK_ENTRY_ENABLED
SM.TICK_ENTRY_ENABLED = False
try:
    st, _ = armed_strat(arm_age_sec=5)
    check("틱 진입이 꺼져 있으면 is_armed_now=False(보수적)",
          not st.is_armed_now("CAND"))
finally:
    SM.TICK_ENTRY_ENABLED = _prev

print("\n[19] 슬롯 교체 발동 조건 3종 (2026-08-03 사용자 지정)")
# ① 슬롯 만석 ② 후보 무장 ③ 후보 버스트 — 셋 다여야 교체.
check("슬롯 만석 요구가 코드에 있음",
      "strat.occupied_slots() < MAX_HOLDINGS" in
      open("core/slot_replacement.py", encoding="utf-8").read())
check("버스트 요구 플래그 ON", _SR2.REQUIRE_CANDIDATE_BURST is True)


def burst_ready(st, code, now=None):
    """후보를 무장 + 버스트 성립 상태로 만든다(3천만원 x2건)."""
    now = now or _tm.time()
    for i in range(2):
        st.phase1b.trade_flow.add_tick(code, 10_000, "buy", 3_000, now=now - i * 0.3)


def full_slots(st, n=None):
    n = n if n is not None else SM.MAX_HOLDINGS
    for i in range(n):
        st.holdings[f"FZ{i}"] = {
            "trade_id": 1, "qty": 1, "buy_price": 10_000,
            "buy_time": NOW - timedelta(minutes=1), "stock_name": f"FZ{i}",
            "sub_strategy": "1A", "warmup_until": None, "entry_strength": 150.0,
            "highest_price": 10_000, "lowest_price": 10_000,
        }


# ① 슬롯 여유면 후보가 완벽해도 교체하지 않는다 (08-03 사고의 직접 원인)
st, now = armed_strat(arm_age_sec=5)
burst_ready(st, "CAND", now)
check("[①] 슬롯 여유(0/6)면 교체 시도 즉시 중단",
      _SR2.try_slot_replacement(st, None, 0, st._now()) == 0)

# ② 슬롯 만석 + 무장 + 버스트 -> 후보 자격 성립
st, now = armed_strat(arm_age_sec=5)
burst_ready(st, "CAND", now)
full_slots(st)
check("[②③] 만석 + 무장 + 버스트면 후보로 선정됨",
      _SR2.find_replacement_candidate(st, 1.0) == ("CAND", 99.0),
      str(_SR2.find_replacement_candidate(st, 1.0)))

# ③ 무장했지만 버스트가 없으면 자격 없음 (오늘 950160이 정확히 이 상태)
st, now = armed_strat(arm_age_sec=5)     # 틱은 소량만 — 버스트 미성립
full_slots(st)
check("[③] 무장했어도 버스트 없으면 후보 자격 없음(950160 패턴)",
      _SR2.find_replacement_candidate(st, 1.0) is None)

print("\n[20] 교체 '대상' 선정 — 진입 스파이크 대신 기준선 사용")
# 08-03 교체 3종목의 진입강도가 300(상한 포화)/100/70이었다. 300에서 시작하면
# `현재 < 진입 x 0.8` 판정을 피할 수 없다 — 결함 ②와 같은 구조.
import core.slot_replacement as _SR3
_ss = open("core/slot_replacement.py", encoding="utf-8").read()
check("교체 대상 판정이 _strength_baseline을 사용",
      "_strength_baseline(pos)" in _ss and "_maybe_anchor_strength_baseline" in _ss)
check("entry_strength 직접 비교가 제거됨",
      'entry_strength = pos.get("entry_strength")' not in _ss)

# 기준선이 없으면(워밍업 직후 등) 교체 대상으로 잡히지 않는다
sT = build()
pT = put_pos(sT, code="STG1", entry_strength=300.0, now_dt=NOW)
pT["buy_time"] = NOW - timedelta(minutes=20)   # 보유시간 조건은 충족
sT._current_strength = lambda c: 5.0           # 진입강도 300 대비 큰 하락
check("기준선 미확보면 교체 대상 아님(구버전은 300->5로 즉시 대상)",
      _SR3.find_stagnant_holding(sT, sT._now()) is None)

# 기준선이 잡히면 그 값 기준으로 판정한다
pT["strength_baseline"] = 120.0
sT._current_strength = lambda c: 110.0         # 120*0.8=96 보다 위 -> 하락 아님
check("기준선 대비 하락이 아니면 교체 대상 아님",
      _SR3.find_stagnant_holding(sT, sT._now()) is None)
sT._current_strength = lambda c: 50.0          # 96 미만 -> 진짜 하락
res = _SR3.find_stagnant_holding(sT, sT._now())
check("기준선 대비 진짜 하락은 정상 감지",
      res is not None and res[0] == "STG1", str(res))

print("\n[14] 죽은 코드 배너 — 이름 때문에 생기는 오해 방지")
_src = open("core/strategy_manager.py", encoding="utf-8").read()
check("PHASE1A_LEADING_SUSTAIN_SEC에 [미사용] 배너",
      "PHASE1A_LEADING_SUSTAIN_SEC = 3        # [미사용]" in _src)
check("evaluate_1a_leading_strength에 DEPRECATED 배너",
      "[삭제 예정 / DEPRECATED — 2026-08-03 확인]"
      in (SM.StrategyManager.evaluate_1a_leading_strength.__doc__ or ""))
check("라이브 무장은 TICK_STRENGTH_SUSTAIN_SEC만 사용",
      "PHASE1A_LEADING_SUSTAIN_SEC" not in
      __import__("inspect").getsource(SM.StrategyManager._maybe_tick_entry))

print("\n[8] daily_backtest 동기화")
import core.daily_backtest as DB
check("백테스트가 라이브 캡을 그대로 참조", DB.TAKE_PROFIT_CAP == SM.TAKE_PROFIT_CAP,
      f"{DB.TAKE_PROFIT_CAP}")
check("백테스트 눌림 캡도 동기", DB.TAKE_PROFIT_CAP_PULLBACK == SM.TAKE_PROFIT_CAP_PULLBACK)
check("백테스트 손절도 동기", DB.STOP_LOSS_RATE == SM.STOP_LOSS_RATE)

print("\n" + "=" * 62)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print(f"  - {f}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
