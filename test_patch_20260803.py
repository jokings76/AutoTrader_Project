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

print("\n[2] 본전스톱 — '익절 조기확정'을 대체")
check("본전스톱 활성", SM.BREAKEVEN_STOP_ENABLED is True)
check("무장 지점 순 +1.0%", abs(SM.BREAKEVEN_TRIGGER - 0.010) < 1e-9)
check("바닥 = 본전(0%)", abs(SM.BREAKEVEN_FLOOR - 0.0) < 1e-9)

s = build()
pos = put_pos(s)
# 순 +1.0% 도달 = 가격 +1.23% (수수료 0.23% 포함)
px_arm = int(10_000 * (1 + SM.BREAKEVEN_TRIGGER + SM.ROUND_TRIP_COST)) + 1
s.on_price_update("000001", px_arm)
check("순 +1% 도달 시 무장 (매도 안 함)",
      pos.get("breakeven_armed") is True and not sold(s), f"@{px_arm}")

# 구버전이라면 여기서 '익절 조기확정'으로 팔았어야 한다.
check("구버전의 '조기확정 매도'가 더 이상 없음", not sold(s))
check("조기확정 문자열이 코드에서 제거됨",
      "익절 조기확정" not in open(
          "core/strategy_manager.py", encoding="utf-8").read().split(
              "# (2026-08-03) rising is False")[0].split("exit_reason = (")[-1])

# 무장 후 본전 아래로 내려오면 청산
s.on_price_update("000001", 10_000)   # 순 -0.23% (수수료만큼 손실)
check("무장 후 본전 이탈 시 청산", sold(s))
check("청산 사유가 본전스톱",
      any("본전스톱" in (r.get("exit_reason") or "") for r in _Repo.sells),
      str([r.get("exit_reason") for r in _Repo.sells])[:70])

# 무장 전에는 본전스톱이 발동하지 않는다(진입 직후 흔들림에 잘리면 안 됨)
s2 = build()
put_pos(s2, code="000002")
s2.on_price_update("000002", 9_990)   # 소폭 하락, +1% 찍은 적 없음
check("무장 전에는 본전스톱 미발동", not sold(s2, "000002"))

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
s6 = build()
p6 = put_pos(s6, code="000006")
s6.on_price_update("000006", px_arm)          # 먼저 무장
check("무장 확인", p6.get("breakeven_armed") is True)
s6.on_price_update("000006", 9_600)           # -4% 급락
check("급락 시 손절로 청산", sold(s6, "000006"))
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
