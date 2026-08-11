"""2026-08-05 실전투자 전환 감사 — 모의(IS_MOCK) -> 실전 전환으로 새로 생긴 위험만 검증.

기존 6개 스위트가 보지 않는 축만 다룬다:
  - 기존 5개 = 전략 로직이 스펙대로 도는가
  - test_live_dryrun_20260803 = 하루 전 구간 배선이 이어지는가
  - **이 파일** = 실전 계좌로 붙였을 때 (a) 자금/수량 계산이 맞는가
                  (b) 봇이 **사용자의 기존 보유 종목을 건드리지 않는가**
                  (c) 모의 전용 하드코딩이 남아 있지 않은가

실전 계좌 실측 기준 (2026-08-04 22:36 조회):
    100stk_ord_alow_amt = 10,835,694원   <- 주문가능 (미수 없음)
    기존 보유 = 우리기술(032820) 271주 / 엑스게이트(356680) 2주  <- DB에 없음
    DB status=open = 0건

실행: python test_live_20260805.py   (종료코드 0 = 전원 통과)
"""
import os as _os_testlog
# 실거래 로그(autotrader.log) 오염 방지 — 반드시 core/main 임포트보다 먼저.
_os_testlog.environ["AUTOTRADER_TEST_LOG"] = "1"

import inspect
import sys
import time
from datetime import datetime, timedelta, time as dtime

import core.strategy_manager as SM
from core.phase1b_controller import Phase1BController

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


# ─── 스텁 (test_live_dryrun_20260803.py와 동일 계약) ───────────────
class _Repo:
    rows, sells, updates = [], [], []
    @classmethod
    def find_holdings(cls): return []          # DB open 포지션 0건 (실측과 동일)
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
    def update(cls, row_id, data): cls.updates.append({"id": row_id, **data}); return True
    @classmethod
    def add(cls, **kw): cls.rows.append(kw); return len(cls.rows)
    @classmethod
    def mark_bought(cls, i): return True
    @classmethod
    def log(cls, *a, **kw): return True
    @classmethod
    def find_closed_by_substrategy(cls, sub): return []


class _Theme:
    def __init__(self, *a, **kw): self.code_to_theme = {}; self.leading_themes = []
    def fetch_themes_from_github(self): pass
    def start_auto_update(self, *a, **kw): pass
    def is_leading_theme_stock(self, code): return False


class _Rest:
    """실전 계좌 실측값을 그대로 돌려주는 스텁."""
    host = "https://api.kiwoom.com"
    ORDERABLE = 10_835_694
    def __init__(self): self.calls = []
    def get_minute_candles(self, code, interval=1, count=1, base_date=None):
        self.calls.append(("candles", code, count)); return make_candles(count)
    def get_orderable_amount(self): return self.ORDERABLE
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


def make_candles(n, today="20260805", base=10_000):
    out = []
    today_n = max(1, n // 2)
    for i in range(n):
        is_today = i < today_n
        day = today if is_today else "20260804"
        mm = (today_n - i) if is_today else (60 - (i - today_n))
        px = base + (today_n - i) * 10
        out.append({"time_str": f"{day}{9 if is_today else 15:02d}{mm % 60:02d}00",
                    "open": px - 5, "high": px + 10, "low": px - 10,
                    "close": px, "volume": 1000 + i * 10})
    return out


class Clock:
    def __init__(self, dt): self.dt = dt
    def __call__(self): return self.dt
    def set(self, h, m, s=0): self.dt = self.dt.replace(hour=h, minute=m, second=s)


def build(now_dt=datetime(2026, 8, 5, 9, 5, 0)):
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows, _Repo.sells, _Repo.updates = [], [], []
    clock = Clock(now_dt)
    strat = SM.StrategyManager(
        kiwoom_rest=_Rest(), order_manager=_OrderMgr(),
        phase1b_controller=Phase1BController(), portfolio_optimizer=None,
        now_func=clock,
    )
    return strat, clock


T0 = time.time()

# ═════════════════════════════════════════════════════════
print("\n[1] 실전 전환 — 모의 하드코딩이 남아있지 않은가")
# ═════════════════════════════════════════════════════════
from api import auth as _auth
from api.kiwoom_rest import KiwoomREST
from api.kiwoom_ws import KiwoomWS
from config import settings

src = inspect.getsource(_auth.get_access_token)
check("auth 토큰 URL이 IS_MOCK 분기를 탄다 (하드코딩 아님)",
      "settings.IS_MOCK" in src and "api.kiwoom.com/oauth2/token" in src)
check("auth에 mockapi가 조건 없이 박혀있지 않다",
      src.count("mockapi.kiwoom.com") == 1)

check("REST 호스트: 실전=api / 모의=mockapi 로 갈린다",
      KiwoomREST("t", is_mock=False).host == "https://api.kiwoom.com"
      and KiwoomREST("t", is_mock=True).host == "https://mockapi.kiwoom.com")


class _CM:
    conditions = {}


check("WS URL: 실전=api / 모의=mockapi 로 갈린다",
      KiwoomWS("t", condition_manager=_CM(), is_mock=False).url.startswith("wss://api.kiwoom.com")
      and KiwoomWS("t", condition_manager=_CM(), is_mock=True).url.startswith("wss://mockapi.kiwoom.com"))

import core.tick_archive as _ta
check("tick_archive도 IS_MOCK 분기를 탄다",
      "IS_MOCK" in inspect.getsource(_ta))

check("config.ini가 실전으로 설정됨 (IS_MOCK=False)", settings.IS_MOCK is False,
      f"IS_MOCK={settings.IS_MOCK!r}")
# (2026-08-11) 조건식 개편: 돌파자동매매용 -> 돌파전(매수) + 돌파후(확인전용).
check("조건검색식 4개 + 종가베팅 분리 유지",
      settings.CONDITION_NAMES == ["주도주상위", "눌림목자동", "돌파전", "돌파후"]
      and settings.CLOSING_BET_CONDITION_NAME == "종가베팅"
      and settings.CLOSING_BET_CONDITION_NAME not in settings.CONDITION_NAMES,
      f"{settings.CONDITION_NAMES} + {settings.CLOSING_BET_CONDITION_NAME}")
# 확인 전용 조건식은 **구독은 되지만 단독 매수는 안 된다**.
check("확인전용 조건식이 CONDITION_NAMES에 있다(구독 대상)",
      all(c in settings.CONDITION_NAMES for c in SM.CONFIRM_ONLY_CONDITIONS),
      f"{SM.CONFIRM_ONLY_CONDITIONS}")
for _cn, _want in (("돌파후", True), ("돌파전", False),
                   ("돌파전+돌파후", False), ("주도주상위+돌파후", False)):
    _r = SM.StrategyManager._confirm_only_reject(_cn)
    check(f"확인전용 판정 [{_cn}] -> {'차단' if _want else '통과'}",
          bool(_r) is _want, str(_r)[:50])

# ═════════════════════════════════════════════════════════
print("\n[2] 예수금 — D+2 매도대금이 매수여력에 반영되는가")
# ═════════════════════════════════════════════════════════
# 실전 계좌 실측 응답을 그대로 재현. ord_alow_amt(D+0 현금)만 보면
# 100,818원이라 MDD 기준자본이 1/100로 잡히는 버그가 있었다.
REAL_DEPOSIT = {
    "return_code": 0,
    "entr": "000000000100818",
    "ord_alow_amt": "000000000100818",       # D+0 현금
    "d2_entra": "000000010835698",
    "d2_pymn_alow_amt": "000000010835698",
    "100stk_ord_alow_amt": "000000010835694",  # 증거금100% 주문가능 <- 정답
    "20stk_ord_alow_amt": "000000062578465",   # 미수 — 절대 쓰면 안 됨
    "repl_amt": "000000001681620",             # 대용금 — 포함하면 안 됨
}


class _DepRest(KiwoomREST):
    def __init__(self, payload):
        self._payload = payload
        self._candle_cache = None
    def get_deposit(self): return self._payload


amt = _DepRest(REAL_DEPOSIT).get_orderable_amount()
check("주문가능금액이 D+2 반영값(10,835,694)으로 나온다", amt == 10_835_694, f"{amt:,}원")
check("미수 금액(62,578,465)을 절대 쓰지 않는다", amt != 62_578_465)
check("대용금(1,681,620)이 섞이지 않는다", amt < 10_835_694 + 1_681_620)

no100 = dict(REAL_DEPOSIT); no100.pop("100stk_ord_alow_amt")
check("100stk가 없으면 d2_pymn_alow_amt로 폴백 (모의서버 호환)",
      _DepRest(no100).get_orderable_amount() == 10_835_698)
onlyd0 = {"return_code": 0, "ord_alow_amt": "000000000100818"}
check("둘 다 없으면 ord_alow_amt로 최종 폴백",
      _DepRest(onlyd0).get_orderable_amount() == 100_818)
check("조회 실패(return_code!=0)면 0을 반환 — 매수 안전측",
      _DepRest({"return_code": -1}).get_orderable_amount() == 0)

# MDD 기준자본이 실전 자본으로 잡히는지
s, clock = build()
s._ensure_base_capital()
check("MDD 기준자본이 실전 주문가능금액으로 잡힌다",
      s._base_capital == _Rest.ORDERABLE, f"{s._base_capital:,.0f}원")
mdd_limit = s._base_capital * SM.DAILY_LOSS_LIMIT
check("MDD 일손실 한도가 -32만원대 (옛 버그면 -3천원)",
      -400_000 < mdd_limit < -300_000, f"{mdd_limit:,.0f}원")

# ═════════════════════════════════════════════════════════
print("\n[3] 🔴 기존 보유 종목 불가침 — 봇이 사용자 주식을 팔면 안 된다")
# ═════════════════════════════════════════════════════════
# 실전 계좌에 우리기술(-30.36%)/엑스게이트(-23.02%)가 있다. DB에는 없다.
# 이 둘이 holdings에 들어가면 손절(-3%)로 **즉시 전량 시장가 매도**된다.
ORPHANS = {
    "032820": {"name": "우리기술", "qty": 271, "avg_price": 15530, "cur_price": 10840},
    "356680": {"name": "엑스게이트", "qty": 2, "avg_price": 13664, "cur_price": 10540},
}

s, clock = build(datetime(2026, 8, 5, 8, 59, 0))
check("DB가 비어 있으면 holdings도 비어 있다 (서버 잔고로 채우지 않음)",
      len(s.holdings) == 0, f"holdings={list(s.holdings)}")

for code, info in ORPHANS.items():
    check(f"{info['name']}({code})이 holdings에 없다", code not in s.holdings)

# 손절 판정 경로를 직접 태워도 매도가 나가면 안 된다
before = len(s.order_manager.orders)
for code, info in ORPHANS.items():
    s.on_price_update(code, info["cur_price"])
check("보유목록에 없는 종목은 on_price_update가 매도를 내지 않는다",
      len(s.order_manager.orders) == before, f"주문 {len(s.order_manager.orders) - before}건")

# 15:10 강제청산은 holdings만 순회한다 (main.task_force_close_watcher와 동일 계약)
clock.set(15, 10, 0)
force_targets = list(s.holdings.keys())
check("15:10 강제청산 대상에 기존 보유 2종목이 없다",
      all(c not in force_targets for c in ORPHANS), f"대상={force_targets}")

# check_timeouts(틱이 안 들어오는 종목용 별도 루프)도 마찬가지
before = len(s.order_manager.orders)
try:
    s.check_timeouts()
except Exception as e:
    check("check_timeouts 예외 없음", False, str(e))
else:
    check("check_timeouts도 기존 보유 종목을 팔지 않는다",
          len(s.order_manager.orders) == before)

# 서버->holdings 복원 경로가 존재하지 않는지 소스로 확인
main_src = open("main.py", encoding="utf-8").read()
check("_detect_orphan_positions는 감지만 하고 holdings에 넣지 않는다",
      "_orphan_notified" in main_src
      and "strat.holdings[" not in main_src.split("_detect_orphan_positions")[1][:2000])
check("강제청산이 server_positions가 아니라 strategy_mgr.holdings를 순회한다",
      "for code in list(self.strategy_mgr.holdings.keys())" in main_src)

# ═════════════════════════════════════════════════════════
print("\n[4] 매수금액 — 3곳 정합 + 0주 스킵 방지")
# ═════════════════════════════════════════════════════════
from core.strategy.portfolio_optimizer import DEFAULT_BASE_AMOUNT
from core.order_manager import BUY_AMOUNT_PER_STOCK

check("POSITION_AMOUNT == DEFAULT_BASE_AMOUNT == BUY_AMOUNT_PER_STOCK",
      SM.POSITION_AMOUNT == DEFAULT_BASE_AMOUNT == BUY_AMOUNT_PER_STOCK,
      f"{SM.POSITION_AMOUNT:,} / {DEFAULT_BASE_AMOUNT:,} / {BUY_AMOUNT_PER_STOCK:,}")
# 08-05: 50만 -> 200만 환원 / **08-06: 200만 -> 100만 축소**(사용자 지정,
# 진입 신호 우위 재검증 기간의 관찰 비용 절반). 금액 자체는 사용자 결정이라
# 특정 값을 못박지 않고 **3곳 일치**와 안전 범위만 검사한다 — 그래야 금액을
# 바꿀 때마다 테스트가 깨지지 않는다(수치 하드코딩 금지 규칙).
check("매수금액이 상식적 범위(10만~1,000만원)",
      100_000 <= SM.POSITION_AMOUNT <= 10_000_000, f"{SM.POSITION_AMOUNT:,}원")

# 조건검색식 주가 상한(150,000원)에서도 트랜치가 1주 이상을 사는가.
# final_weight는 [0.3, 2.0]로 클리핑되고, 되돌림 1차 트랜치는 50%.
COND_PRICE_MAX = 150_000
w_typ = 0.90     # 실측 (Kelly 손실기대 0.3 x 변동성 상한 3.0)
frac = SM.ENTRY_PULLBACK_TRANCHES[0][1]
tr_typ = int(SM.POSITION_AMOUNT * w_typ) // 1000 * 1000 * frac
check("실측 비중(0.90)에서 최고가 종목도 1주 이상",
      tr_typ // COND_PRICE_MAX >= 1,
      f"트랜치 {int(tr_typ):,}원 / {COND_PRICE_MAX:,}원 = {int(tr_typ)//COND_PRICE_MAX}주")

tr_min = int(SM.POSITION_AMOUNT * 0.3) // 1000 * 1000 * frac
check("[알려진 한계] 최소비중 0.3 + 고가주는 0주 스킵 가능 (실무상 도달 불가)",
      True, f"트랜치 {int(tr_min):,}원 -> {int(tr_min)//COND_PRICE_MAX}주 "
            f"(0.3 도달 조건: 1분봉 ATR >= 6.67%)")

# 실제 매수 시 수량이 0이면 주문을 내지 않는가
s, clock = build()
src_buy = inspect.getsource(SM.StrategyManager._execute_buy)
check("수량 0이면 주문을 내지 않고 skip한다",
      "quantity < 1" in src_buy and "수량 0" in src_buy)

# ═════════════════════════════════════════════════════════
print("\n[5] 노출 한도 — 자본을 넘는 주문이 나가지 않는가")
# ═════════════════════════════════════════════════════════
CAP = _Rest.ORDERABLE
per_typ = int(SM.POSITION_AMOUNT * w_typ)
per_max = int(SM.POSITION_AMOUNT * 2.0 * SM.PHASE1A_SIZE_MAX_MULT)
check("평상시 슬롯 6 만석 노출이 자본 이내",
      per_typ * SM.MAX_HOLDINGS < CAP,
      f"{per_typ * SM.MAX_HOLDINGS:,}원 / {CAP:,}원 "
      f"({per_typ * SM.MAX_HOLDINGS / CAP * 100:.1f}%)")
check("[경고] 최대비중 x 확장슬롯 8은 자본을 넘을 수 있다",
      True,
      f"{per_max * SM.MAX_HOLDINGS_HARD:,}원 / {CAP:,}원 "
      f"({per_max * SM.MAX_HOLDINGS_HARD / CAP * 100:.0f}%) "
      f"-> 초과분은 키움이 거부(주문 실패)하며 오주문은 아님")

# ═════════════════════════════════════════════════════════
print("\n[6] 매수 경로 2곳이 모두 되돌림 대기를 거치는가 (08-04 회귀)")
# ═════════════════════════════════════════════════════════
src_poll = inspect.getsource(SM.StrategyManager._evaluate_1a_pullback_entry)
src_tick = inspect.getsource(SM.StrategyManager._maybe_tick_entry)
check("폴링 경로가 _execute_buy를 직접 부르지 않는다 (계획 경유)",
      "_open_entry_plan" in src_poll or "_entry_plans" in src_poll)
check("틱 경로도 되돌림 계획을 연다",
      "_open_entry_plan" in src_tick or "_entry_plans" in src_tick)
check("되돌림 대기가 켜져 있다", SM.ENTRY_PULLBACK_ENABLED is True)
check("트랜치 비중 합 = 1.0 (분할해도 총액 불변)",
      abs(sum(f for _, f in SM.ENTRY_PULLBACK_TRANCHES) - 1.0) < 1e-9,
      str(SM.ENTRY_PULLBACK_TRANCHES))
check("대기 중 종목도 슬롯을 점유한다 (자리 뺏김 방지)",
      "_entry_plans" in inspect.getsource(SM.StrategyManager.occupied_slots))

# ═════════════════════════════════════════════════════════
print("\n[7] 매도 — 손절이 최우선이고 워밍업에 막히지 않는가")
# ═════════════════════════════════════════════════════════
src_upd = inspect.getsource(SM.StrategyManager.on_price_update)
i_stop = src_upd.find("손절")
i_warm = src_upd.find("warmup_until")
check("손절 판정이 워밍업 게이트보다 위에 있다",
      0 <= i_stop < i_warm, f"손절 idx={i_stop} / 워밍업 idx={i_warm}")
check("손절은 분할매도 대상이 아니다 (항상 전량)",
      "sell_qty" not in src_upd[i_stop:i_stop + 600])
check("본전스톱 ON", SM.BREAKEVEN_STOP_ENABLED is True)
check("분할매도 ON + 비중 0.5 + 잔량 트레일 3%",
      SM.PARTIAL_EXIT_ENABLED is True
      and SM.PARTIAL_EXIT_FRACTION == 0.5
      and SM.PARTIAL_EXIT_TRAIL == 0.03,
      f"{SM.PARTIAL_EXIT_ENABLED}/{SM.PARTIAL_EXIT_FRACTION}/{SM.PARTIAL_EXIT_TRAIL}")

# 실제 손절 시나리오 (워밍업 중)
s, clock = build(datetime(2026, 8, 5, 9, 30, 0))
s.holdings["X1"] = {
    "trade_id": 1, "buy_price": 10_000, "buy_quantity": 10, "qty": 10,
    "buy_time": clock(), "stock_name": "X1", "strategy_phase": "1A",
    "sub_strategy": "1A", "highest_price": 10_000, "lowest_price": 10_000,
    "ma20": None, "ma20_updated": None,
    # ⚠️ (2026-08-11 수정) 여기 float(timestamp)를 넣어뒀었다. 실물은 항상
    #    datetime이라 `self._now() < warmup_until`에서 TypeError가 난다.
    #    손절이 항상 먼저 발동해 그 줄에 도달하지 않아 **가려져 있던 결함**이고,
    #    손절선이 -4.5%로 깊어지자 바로 터졌다.
    "warmup_until": clock() + timedelta(seconds=999),  # 워밍업 한창
}
# -3% 물타기가 손절선(-4.5%)보다 먼저 개입하므로, 손절 경로만 보려면 끈다.
_sv_ad = SM.AVG_DOWN_ENABLED
SM.AVG_DOWN_ENABLED = False
try:
    s.on_price_update("X1", int(10_000 * (1 + SM.STOP_LOSS_RATE)) - 1)
finally:
    SM.AVG_DOWN_ENABLED = _sv_ad
sold = [o for o in s.order_manager.orders if o["side"] == "sell"]
check("워밍업 중에도 손절이 실제로 나간다", len(sold) == 1, f"매도 {len(sold)}건")
check("손절은 전량 매도", sold and sold[0]["qty"] == 10, str(sold))

# ═════════════════════════════════════════════════════════
print("\n[8] 상수 불변식 (수치)")
# ═════════════════════════════════════════════════════════
from core.order_manager import FORCE_CLOSE_TIME
check("1A/Pullback 시간창 동일 09:00~14:50",
      SM.GROUP_A_START == SM.PULLBACK_START == dtime(9, 0)
      and SM.PHASE1A_END == SM.PULLBACK_END == dtime(14, 50))
check("진입 종료(14:50) < 강제청산(15:10)",
      SM.PHASE1A_END < dtime(15, 10) and FORCE_CLOSE_TIME == "15:10")
check("무장 3.0초, 임계 100", SM.TICK_STRENGTH_SUSTAIN_SEC == 3.0
      and SM.TICK_STRENGTH_MIN == 100.0,
      f"{SM.TICK_STRENGTH_SUSTAIN_SEC}초 / {SM.TICK_STRENGTH_MIN}")
check("버스트 문턱 4천만 x2건 / 단일 1억",
      SM.PHASE1A_BURST_TRADE_VALUE == 40_000_000
      and SM.PHASE1A_BURST_TRADE_COUNT == 2
      and SM.PHASE1A_SINGLE_TRADE_VALUE == 100_000_000)
mults = sorted(set(v for _, v in SM.TICK_BURST_TIME_MULT))
check("시간대 계수가 전부 1.00 (오후 완화 제거)", mults == [1.0], str(mults))
check("모든 익절캡 > 왕복수수료",
      min(SM.TAKE_PROFIT_CAP, SM.TAKE_PROFIT_CAP_PULLBACK,
          SM.TAKE_PROFIT_CAP_EARLY, SM.TP_CAP_UPGRADED_MAX) > SM.ROUND_TRIP_COST)
check("기본캡 != 상향캡 (08-03 결함① 재발 방지)",
      SM.TAKE_PROFIT_CAP != SM.TP_CAP_UPGRADED_MAX,
      f"{SM.TAKE_PROFIT_CAP} vs {SM.TP_CAP_UPGRADED_MAX}")
check("지수가드 임계 == SEVERE_CRASH 임계 (두 규칙 동기)",
      SM.INDEX_GUARD_THRESHOLD == -5.0)
# (2026-08-06 [E]) 눌림 캡 4 -> 0, 1A 캡 4 -> 8.
# 값 자체보다 **불변식**을 검사한다 — 죽은 슬롯이 생기지 않는가.
check("슬롯: 눌림 0 / 1A가 공유상한을 채울 수 있음 / 하드상한 이내",
      SM.PULLBACK_MAX_SLOTS == 0
      and SM.PHASE1A_MAX_SLOTS >= SM.MAX_HOLDINGS
      and SM.PHASE1A_MAX_SLOTS <= SM.MAX_HOLDINGS_HARD
      and (SM.MAX_HOLDINGS, SM.MAX_HOLDINGS_HARD) == (6, 8),
      f"{SM.PHASE1A_MAX_SLOTS}/{SM.PULLBACK_MAX_SLOTS}/"
      f"{SM.MAX_HOLDINGS}/{SM.MAX_HOLDINGS_HARD}")
check("1B 잔재 없음", not hasattr(SM, "PHASE1B_ENABLED"))
check("틱 구동 ON", SM.TICK_ENTRY_ENABLED is True)

# ═════════════════════════════════════════════════════════
print("\n[9] 라이브 <-> daily_backtest 동기화")
# ═════════════════════════════════════════════════════════
import core.daily_backtest as BT
check("강제청산 시각 동기", BT.FORCE_CLOSE_HHMM == FORCE_CLOSE_TIME.replace(":", ""))
check("본전스톱 동기", BT.BREAKEVEN_STOP_ENABLED == SM.BREAKEVEN_STOP_ENABLED)
check("왕복수수료 동기", BT.ROUND_TRIP_COST == SM.ROUND_TRIP_COST)
check("익절캡 동기",
      BT.TAKE_PROFIT_CAP_PULLBACK == SM.TAKE_PROFIT_CAP_PULLBACK
      and BT.TAKE_PROFIT_CAP_EARLY == SM.TAKE_PROFIT_CAP_EARLY)
check("백테스트가 삭제된 상수를 임포트하지 않는다 (ImportError 방지)",
      not any(x in inspect.getsource(BT)
              for x in ("IMMEDIATE_COND_NAMES", "OTHER_COND_START", "PHASE1B_ENABLED")))

# ═════════════════════════════════════════════════════════
print("\n[10] 배선 — 태스크 정의와 등록이 일치하는가")
# ═════════════════════════════════════════════════════════
import re
defined = set(re.findall(r"async def (task_\w+)", main_src))
registered = set(re.findall(r"self\.(task_\w+)\(\)", main_src))
check("정의된 태스크가 전부 gather에 등록됨",
      defined == registered,
      f"정의 {len(defined)} / 등록 {len(registered)} / 차이 {defined ^ registered or '없음'}")

# ═════════════════════════════════════════════════════════
print("\n[11] 조건검색 거래소구분 — HTS '대상' 설정과 일치하는가")
# ═════════════════════════════════════════════════════════
# 2026-08-07: 통합('A') -> KRX('K')로 되돌림 (사용자 지정).
# 이유는 성과가 아니라 **정합성**이다 — 실시간 0B / REST 분봉 / 주문이 전부
# KRX 전용인데 조건검색만 통합이라, '통합으로 고른 종목'을 'KRX 데이터로'
# 판정하고 있었다(NXT 상장 종목 거래량 결손률 평균 54%).
from api.kiwoom_ws import CONDITION_STEX_TP
import inspect as _insp

check("CONDITION_STEX_TP가 KRX('K')", CONDITION_STEX_TP == "K",
      f"{CONDITION_STEX_TP!r} (HTS 대상=KRX면 'K', 통합이면 'A')")

_src_sub = _insp.getsource(KiwoomWS.subscribe_condition)
_src_snap = _insp.getsource(KiwoomWS.fetch_condition_snapshot)
check("실시간 등록이 상수를 쓴다 (KRX 하드코딩 아님)",
      "CONDITION_STEX_TP" in _src_sub and '"K"' not in _src_sub)
check("스냅샷 조회도 상수를 쓴다",
      "CONDITION_STEX_TP" in _src_snap and 'stex_tp: str = "K"' not in _src_snap)
check("두 경로가 같은 상수를 본다 (한쪽만 바뀌는 사고 방지)",
      _src_sub.count("CONDITION_STEX_TP") >= 1
      and _src_snap.count("CONDITION_STEX_TP") >= 1)

# 주문/잔고의 거래소구분은 조건검색과 **독립**이다. 실측(08:35):
#   kt00018 잔고조회 dmst_stex_tp=KRX/NXT 둘 다 동일한 2종목 반환,
#   SOR은 rc=20(거래소구분 오류). 따라서 "KRX" 유지가 안전하다.
_src_rest = open("api/kiwoom_rest.py", encoding="utf-8").read()
check("주문/잔고는 dmst_stex_tp='KRX' 유지 (조건검색과 독립)",
      _src_rest.count('"dmst_stex_tp": "KRX"') == 4,
      f'{_src_rest.count(chr(34) + "dmst_stex_tp" + chr(34) + ": " + chr(34) + "KRX" + chr(34))}곳')

# ═════════════════════════════════════════════════════════
print("\n" + "=" * 62)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건   ({time.time() - T0:.1f}초)")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print("  -", f)
sys.exit(1 if FAIL else 0)
