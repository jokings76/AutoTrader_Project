"""2026-08-01 패치 격리 검증 (1군+2군 수정 / 1B 비활성화 / 1A 진입조건 재설계 /
호가 기반 하이브리드 주문 / 전략 라우팅 배타화).

네트워크·DB·키움 API를 전혀 타지 않는 순수 격리 테스트.
실행: python test_patch_20260801.py   (종료코드 0 = 전원 통과)
"""
import asyncio
import sys
import time
from datetime import datetime, timedelta

import core.strategy_manager as SM
from core.phase1b_controller import Phase1BController
from core.strategy.trade_flow import TradeFlowTracker, STRENGTH_NEUTRAL
from core.strategy.orderbook import OrderbookTracker

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


# ─────────────────────────────────────────────────────────
# 공용 스텁
# ─────────────────────────────────────────────────────────
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
    def get_stock_change_rate(self, code): return 3.0
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
    def sell(self, code, qty, price=0):
        self.orders.append({"code": code, "qty": qty, "style": "sell"})
        return {"success": True, "ord_no": "2"}
    def get_stock_name(self, code): return code


def make_candles(n, today="20260803", base=10_000, rising=True):
    """내림차순(최신->과거) 1분봉. 앞쪽 n//2는 당일, 나머지는 전일로 채워
    실제 키움 응답(개장 직후 전일 봉이 섞여 옴)을 재현한다."""
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


def build_strat(now_dt=datetime(2026, 8, 3, 9, 5, 0)):
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows = []
    _Repo.sells = []
    ctrl = Phase1BController()
    strat = SM.StrategyManager(
        kiwoom_rest=_Rest(), order_manager=_OrderMgr(),
        phase1b_controller=ctrl, portfolio_optimizer=None,
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


# ═════════════════════════════════════════════════════════
print("\n[1] WS 조건검색 파서 — 실시간 편입(type='02') 유실 수정")
# ═════════════════════════════════════════════════════════
from api.kiwoom_ws import KiwoomWS

def ws_dispatch(msg):
    got = []
    async def on_signal(code, sig, raw, seq=None): got.append((code, sig, seq))
    ws = KiwoomWS.__new__(KiwoomWS)
    ws.on_signal = on_signal
    ws.on_trade = ws.on_orderbook = ws.on_program = None
    ws._cond_keys_logged = True
    ws._seen_item_types = {"02", None, "0B", "0D", "0g"}
    ws._real_empty_logged = True
    asyncio.run(_run(ws, msg))
    return got

async def _run(ws, msg):
    await ws._dispatch_signal(msg)
    await asyncio.sleep(0.05)

# 07-31 실제 로그의 편입 이벤트 원본
got = ws_dispatch({"trnm": "REAL", "data": [{
    "values": {"841": "3", "9001": "079650", "843": "I", "20": "100621"},
    "type": "02", "name": "조건검색", "item": "079650"}]})
check("type='02' 편입 이벤트가 콜백까지 도달", got == [("079650", "I", "3")], str(got))

got = ws_dispatch({"trnm": "REAL", "data": [{
    "values": {"841": "1", "9001": "001210", "843": "D", "20": "095654"},
    "type": "02", "name": "조건검색", "item": "001210"}]})
check("type='02' 이탈(D) 이벤트도 정상 전달", got == [("001210", "D", "1")], str(got))

got = ws_dispatch({"trnm": "CNSRREQ", "data": [
    {"9001": "A002990", "302": "금호건설", "10": "000010220"}]})
check("기존 플랫 구조(등록응답)도 계속 동작", got == [("002990", "I", None)], str(got))

got = ws_dispatch({"trnm": "REAL", "data": [{
    "values": {"843": "I"}, "type": "02", "item": "A123456"}]})
check("9001 없으면 item 필드로 종목코드 폴백(A 제거)", got == [("123456", "I", None)], str(got))

got = ws_dispatch({"trnm": "REAL", "data": [{"values": {}, "type": "999", "item": ""}]})
check("종목코드 없는 잡음 메시지는 무시", got == [], str(got))

# ═════════════════════════════════════════════════════════
print("\n[2] TradeFlowTracker 신규 메서드 (1A 대량체결 버스트)")
# ═════════════════════════════════════════════════════════
tf = TradeFlowTracker()
now = time.time()
tf.add_tick("A", 10_000, "buy", 3_000, now=now - 0.5)   # 3천만
tf.add_tick("A", 10_000, "buy", 3_000, now=now - 1.0)   # 3천만
tf.add_tick("A", 10_000, "buy", 3_000, now=now - 1.5)   # 3천만
tf.add_tick("A", 10_000, "buy", 100, now=now - 2.0)     # 100만
check("count_large_trades: 3천만+ 체결 3건",
      tf.count_large_trades("A", 3, 30_000_000, now=now) == 3)
check("count_large_trades: 창 밖(0.5초)은 제외",
      tf.count_large_trades("A", 0.7, 30_000_000, now=now) == 1)
check("max_single_trade_value = 3천만",
      tf.max_single_trade_value("A", 3, now=now) == 30_000_000)
check("tick_count(3초) = 4", tf.tick_count("A", 3, now=now) == 4)
check("데이터 없는 종목: count 0 / max 0.0 / tick 0",
      tf.count_large_trades("Z", 3, 1) == 0 and tf.max_single_trade_value("Z", 3) == 0.0
      and tf.tick_count("Z", 3) == 0)

tf2 = TradeFlowTracker()
for i in range(3):
    tf2.add_tick("B", 10_000, "buy", 10, now=now - i * 0.1)
check("compute_strength: 기본 min_ticks(5) 미달 -> 중립값",
      tf2.compute_strength("B", 3, now=now) == STRENGTH_NEUTRAL)
check("compute_strength: min_ticks=3 지정 시 실제 계산(매수만이면 상한 300)",
      tf2.compute_strength("B", 3, now=now, min_ticks=3) == 300.0)

# ═════════════════════════════════════════════════════════
print("\n[3] OrderbookTracker.get_ask_depth_value")
# ═════════════════════════════════════════════════════════
ob = OrderbookTracker()
check("스냅샷 없으면 None (0.0과 구분)", ob.get_ask_depth_value("A") is None)
ob.update("A", {"ask_prices": [10_000, 10_010, 10_020, 10_030],
                "ask_volumes": [1_000, 2_000, 3_000, 9_999]}, now=now)
val = ob.get_ask_depth_value("A", levels=3)
check("매도 1~3호가 금액 = 6,006만원", abs(val - (10_000*1000 + 10_010*2000 + 10_020*3000)) < 1,
      f"{val:,.0f}")
check("levels=3이면 4호가는 제외됨", val < 10_030 * 9_999)
ob.update("B", {"ask_prices": [10_000, 10_010], "ask_volumes": [500]}, now=now)
check("호가/잔량 길이 불일치해도 예외 없이 계산", ob.get_ask_depth_value("B", 3) == 5_000_000)

# ═════════════════════════════════════════════════════════
print("\n[4] _today_open — 전일 봉 섞여도 당일 시가")
# ═════════════════════════════════════════════════════════
s = build_strat(datetime(2026, 8, 3, 9, 1, 0))
candles = [
    {"time_str": "202608030901" + "00", "open": 11_000, "close": 11_100},
    {"time_str": "202607311509" + "00", "open": 9_000, "close": 9_100},
    {"time_str": "202607311508" + "00", "open": 8_900, "close": 8_950},
]
check("당일 첫 봉의 시가(11,000)를 반환", s._today_open(candles) == 11_000,
      str(s._today_open(candles)))
check("(구버전 버그 재현) candles[-1]['open']은 전일값 8,900", candles[-1]["open"] == 8_900)
check("당일 봉이 없으면 0.0 (필터 스킵)", s._today_open(candles[1:]) == 0.0)
check("빈 리스트도 안전하게 0.0", s._today_open([]) == 0.0)

# ═════════════════════════════════════════════════════════
print("\n[5] tick() 감시 정리 — 10:30 이후에도 1A 틱버퍼 유지")
# ═════════════════════════════════════════════════════════
s = build_strat(datetime(2026, 8, 3, 10, 35, 0))
s.watch_list_today.add("CAND")
s.phase1b.start_watching("CAND")
s.phase1b.start_watching("HOLD")
s.phase1b.start_watching("STALE")
s.holdings["HOLD"] = {"buy_price": 10_000, "buy_quantity": 1, "buy_time": s._now(),
                      "stock_name": "H", "highest_price": 10_000,
                      "warmup_until": s._now() + timedelta(hours=1), "entry_strength": 0}
for c in ("CAND", "HOLD", "STALE"):
    feed(s.phase1b.trade_flow, c, 5, 1_000_000)
s.tick()
check("10:30 이후에도 후보(CAND) 감시 유지", s.phase1b.is_watching("CAND"))
check("10:30 이후에도 후보 틱버퍼 보존(구버전은 여기서 리셋)",
      s.phase1b.trade_flow.tick_count("CAND", 60) == 5)
check("보유종목(HOLD) 감시 유지", s.phase1b.is_watching("HOLD"))
check("후보도 보유도 아닌 종목(STALE)만 해제", not s.phase1b.is_watching("STALE"))

s2 = build_strat(datetime(2026, 8, 3, 14, 55, 0))   # 1A 창 종료 후
s2.watch_list_today.add("CAND")
s2.phase1b.start_watching("CAND")
s2.tick()
check("1A 창(14:50) 종료 후에는 후보도 정리", not s2.phase1b.is_watching("CAND"))

# ═════════════════════════════════════════════════════════
print("\n[6] on_trade — 보유 종목도 체결틱이 계속 쌓이는지")
# ═════════════════════════════════════════════════════════
s = build_strat()
s.phase1b.start_watching("H1")
s.holdings["H1"] = {"buy_price": 10_000, "buy_quantity": 1, "buy_time": s._now(),
                    "stock_name": "H", "highest_price": 10_000,
                    "warmup_until": s._now() + timedelta(hours=1), "entry_strength": 150}
for i in range(6):
    s.on_trade({"stock_code": "H1", "price": 10_000, "side": "buy", "volume": 10})
check("보유 종목 틱 6건 적재됨 (구버전은 0건)",
      s.phase1b.trade_flow.tick_count("H1", 60) == 6,
      str(s.phase1b.trade_flow.tick_count("H1", 60)))
check("보유 종목 체결강도가 중립값이 아닌 실제값",
      s._current_strength("H1") != STRENGTH_NEUTRAL, str(s._current_strength("H1")))

s.phase1b.start_watching("N1")
for i in range(6):
    s.on_trade({"stock_code": "N1", "price": 10_000, "side": "sell", "volume": 10})
check("미보유 감시종목도 정상 적재", s.phase1b.trade_flow.tick_count("N1", 60) == 6)
before = len(s.order_manager.orders)
feed(s.phase1b.trade_flow, "N1", 5, 1_000_000, now=time.time() - 50)
s.on_trade({"stock_code": "N1", "price": 5_000, "side": "sell", "volume": 10})
check("1B 하락 트리거는 비활성화되어 매수 시도 없음",
      len(s.order_manager.orders) == before and not s._1b_confirm)

# ═════════════════════════════════════════════════════════
print("\n[7] on_orderbook — 호가 적재 재활성화")
# ═════════════════════════════════════════════════════════
s = build_strat()
s.phase1b.start_watching("OB1")
s.on_orderbook({"stock_code": "OB1", "ask_prices": [10_000, 10_010, 10_020],
                "ask_volumes": [1_000, 1_000, 1_000],
                "bid_prices": [9_990], "bid_volumes": [500]})
check("감시종목 호가가 트래커에 적재됨",
      s.phase1b.orderbook.get_ask_depth_value("OB1", 3) is not None)
s.on_orderbook({"stock_code": "NOPE", "ask_prices": [1], "ask_volumes": [1]})
check("미감시 종목 호가는 무시(메모리 낭비 방지)",
      s.phase1b.orderbook.get_ask_depth_value("NOPE") is None)
s.on_orderbook({"stock_code": None})
check("stock_code 없는 메시지에도 예외 없음", True)

# ═════════════════════════════════════════════════════════
print("\n[8] 1A 진입 평가 — 강도 3초 + 대량체결 버스트")
# ═════════════════════════════════════════════════════════
def eval_1a(setup, cond="주도주상위", open_px=0.0, px=10_000):
    st = build_strat()
    st.phase1b.start_watching("X")
    setup(st.phase1b.trade_flow)
    return st.evaluate_1a_leading_strength("X", px, open_px, cond)

ok, info = eval_1a(lambda tf: None)
check("틱 전혀 없으면 탈락", not ok and "대량체결 부족" in info["reason"], info.get("reason", ""))

ok, info = eval_1a(lambda tf: feed(tf, "X", 10, 5_000_000))   # 500만 x10 = 누적 5천만
check("누적 거래대금만 크고 대량체결 없으면 탈락(구버전은 통과)",
      not ok and "대량체결 부족" in info["reason"], info.get("reason", ""))

ok, info = eval_1a(lambda tf: feed(tf, "X", 3, 30_000_000))
check("3천만 x3건 -> 통과", ok, info.get("reason", ""))
check("통과 info에 score/score_threshold 포함(확장슬롯·슬롯교체용)",
      info.get("score") is not None and info.get("score_threshold"), str(info.get("score")))

ok, info = eval_1a(lambda tf: feed(tf, "X", 2, 30_000_000))
check("3천만 x2건(3건 미달) -> 탈락", not ok, info.get("reason", ""))

def _single(tf):
    feed(tf, "X", 1, 100_000_000)
    feed(tf, "X", 2, 1_000_000)   # 강도 판정용 틱 채우기
ok, info = eval_1a(_single)
check("단일 1억 체결 1건 -> 통과(OR 조건)", ok, info.get("reason", ""))
check("통과 사유에 단일체결 트리거 명시", "단일체결" in info.get("reason", ""))

def _sell(tf):
    feed(tf, "X", 3, 30_000_000, side="sell")
ok, info = eval_1a(_sell)
check("대량체결이 전부 매도우세면 강도 미달로 탈락",
      not ok and "체결강도 미달" in info["reason"], info.get("reason", ""))

def _thin(tf):
    tf.add_tick("X", 10_000, "buy", 10_000, now=time.time())   # 1억 1건뿐
ok, info = eval_1a(_thin)
check("단일 1억이어도 틱 3개 미만이면 강도 판단불가로 탈락",
      not ok and "체결틱 부족" in info["reason"], info.get("reason", ""))

ok, info = eval_1a(lambda tf: feed(tf, "X", 3, 30_000_000), open_px=10_000, px=10_600)
check("주도주상위 시가대비 +6% -> 매수보류", not ok and "시가대비" in info["reason"],
      info.get("reason", ""))
ok, info = eval_1a(lambda tf: feed(tf, "X", 3, 30_000_000), open_px=10_000, px=10_400)
check("시가대비 +4%는 통과", ok, info.get("reason", ""))
ok, info = eval_1a(lambda tf: feed(tf, "X", 3, 30_000_000), cond="돌파자동매매용",
                   open_px=10_000, px=10_600)
check("시가급등 필터는 주도주상위 전용(돌파자동매매용은 통과)", ok, info.get("reason", ""))

# ═════════════════════════════════════════════════════════
print("\n[9] 하이브리드 주문 방식 판정")
# ═════════════════════════════════════════════════════════
s = build_strat()
s.phase1b.start_watching("M1")
s.phase1b.orderbook.update("M1", {"ask_prices": [10_000, 10_010, 10_020],
                                  "ask_volumes": [3_000, 3_000, 3_000]}, now=now)
style, ref, why = s._resolve_order_style("M1", 10_000)
check("매도1~3호가 9천만원(>=5천만) -> 시장가", style == "market", why)
check("시장가 기준가 = 매도1호가", ref == 10_000, str(ref))

s.phase1b.start_watching("L1")
s.phase1b.orderbook.update("L1", {"ask_prices": [10_000, 10_010, 10_020],
                                  "ask_volumes": [100, 100, 100]}, now=now)
style, ref, why = s._resolve_order_style("L1", 10_000)
check("매도1~3호가 300만원(<5천만, 빈 호가창) -> 지정가", style == "limit", why)

style, ref, why = s._resolve_order_style("NONE", 10_000)
check("호가 스냅샷 없음 -> 지정가(보수적)", style == "limit", why)

class _BadOB:
    def get_ask_depth_value(self, *a, **kw): raise RuntimeError("boom")
    def get_top_ask(self, *a, **kw): raise RuntimeError("boom")
s.phase1b.orderbook = _BadOB()
style, ref, why = s._resolve_order_style("M1", 10_000)
check("호가 판정 중 예외 발생해도 지정가로 안전 수렴(매수 자체는 계속)",
      style == "limit", why)

# 실제 _execute_buy까지 흐르는지
s = build_strat()
s.phase1b.start_watching("M2")
s.phase1b.orderbook.update("M2", {"ask_prices": [10_000, 10_010, 10_020],
                                  "ask_volumes": [3_000, 3_000, 3_000]}, now=now)
feed(s.phase1b.trade_flow, "M2", 5, 1_000_000)
s._execute_buy("M2", "테스트", 1, {"current_price": 10_000, "score": 150.0,
                                   "score_threshold": 100.0}, "1A")
check("_execute_buy가 시장가로 주문 전달",
      s.order_manager.orders and s.order_manager.orders[-1]["style"] == "market",
      str(s.order_manager.orders))
check("매수단가가 주문 결과 가격(매도1호가)으로 기록",
      s.holdings["M2"]["buy_price"] == 10_000, str(s.holdings.get("M2", {}).get("buy_price")))
check("entry_score가 컷라인 대비 비율(1.5)로 기록",
      abs(s.holdings["M2"]["entry_score"] - 1.5) < 1e-9,
      str(s.holdings["M2"]["entry_score"]))
check("lowest_price 초기화됨(손실반등 매도 전제조건)",
      s.holdings["M2"].get("lowest_price") == 10_000)

s = build_strat()
s.phase1b.start_watching("L2")
s.phase1b.orderbook.update("L2", {"ask_prices": [10_000], "ask_volumes": [10]}, now=now)
feed(s.phase1b.trade_flow, "L2", 5, 1_000_000)
s._execute_buy("L2", "테스트", 1, {"current_price": 10_000}, "1A")
check("빈 호가창에서는 지정가로 주문",
      s.order_manager.orders and s.order_manager.orders[-1]["style"] == "limit")

# ═════════════════════════════════════════════════════════
print("\n[10] 전략 라우팅 배타성 (눌림목매수는 눌림목자동에서만)")
# ═════════════════════════════════════════════════════════
def route(cond_name, now_dt=datetime(2026, 8, 3, 9, 5, 0)):
    st = build_strat(now_dt)
    calls = []
    st.evaluate_1a_leading_strength = lambda *a, **k: (calls.append("1A"), (False, {"reason": "x"}))[1]
    st.evaluate_pullback = lambda *a, **k: (calls.append("PB"), (False, {"reason": "x"}))[1]
    st._cond_names["R"] = cond_name
    st._evaluate_1a_pullback_entry("R", "R", 1, make_candles(15), 10_000, 9_900,
                                   now_dt.time())
    return calls

check("눌림목자동 단독 -> Pullback만 평가", route("눌림목자동") == ["PB"], str(route("눌림목자동")))
check("눌림목자동+주도주상위 -> Pullback만 (1A 평가 안 함)",
      route("주도주상위+눌림목자동") == ["PB"], str(route("주도주상위+눌림목자동")))
check("주도주상위 -> 1A만 평가", route("주도주상위") == ["1A"], str(route("주도주상위")))
check("돌파자동매매용 -> 1A만 평가 (09:20 이후)",
      route("돌파자동매매용", datetime(2026, 8, 3, 9, 25, 0)) == ["1A"])
check("돌파자동매매용은 09:20 전엔 평가 보류",
      route("돌파자동매매용", datetime(2026, 8, 3, 9, 5, 0)) == [])
check("눌림목자동은 10:30 이후 Pullback 창 종료로 평가 없음",
      route("눌림목자동", datetime(2026, 8, 3, 11, 0, 0)) == [])

# ═════════════════════════════════════════════════════════
print("\n[11] 장 시작 전(08:59) 편입 — 폐기 대신 후보 등록")
# ═════════════════════════════════════════════════════════
s = build_strat(datetime(2026, 8, 3, 8, 59, 30))
s.on_condition_hit("PRE1", "프리장", cond_name="주도주상위")
check("08:59 편입 종목이 후보로 등록됨(구버전은 폐기)", "PRE1" in s.watch_list_today)
check("종목명 보존", s._stock_names.get("PRE1") == "프리장")
check("조건검색식 이름 보존", s._cond_names.get("PRE1") == "주도주상위")
check("장 시작 전에는 REST 분봉 조회 안 함(호출 예산 절약)",
      not any(c[1] == "PRE1" for c in s.api.calls), str(s.api.calls))

s2 = build_strat(datetime(2026, 8, 3, 14, 55, 0))
s2.on_condition_hit("LATE", "장막판", cond_name="주도주상위")
check("1A 창 종료 후 편입은 평가 안 함", "LATE" not in s2.watch_list_today)
check("그래도 이름/조건명은 기록", s2._cond_names.get("LATE") == "주도주상위")

# ═════════════════════════════════════════════════════════
print("\n[12] 점수 정규화 + 워치리스트 DB 기록 분리")
# ═════════════════════════════════════════════════════════
s = build_strat()
check("_score_ratio: 150/100 = 1.5",
      abs(s._score_ratio({"score": 150, "score_threshold": 100}) - 1.5) < 1e-9)
check("_score_ratio: 7.5/5.0 = 1.5 (스케일 달라도 동일 비율)",
      abs(s._score_ratio({"score": 7.5, "score_threshold": 5.0}) - 1.5) < 1e-9)
check("_score_ratio: 점수 없으면 0.0", s._score_ratio({}) == 0.0)
check("_score_ratio: 컷라인 0이면 0.0 (0 나눗셈 방지)",
      s._score_ratio({"score": 5, "score_threshold": 0}) == 0.0)

before = len(_Repo.rows)
s.watch_list_today.add("W1")          # 지연평가 경로가 먼저 후보 등록한 상황
s._record_watch_list("W1", "W", 1, {"score": 120, "score_threshold": 100}, "주도주상위")
check("후보에 이미 있어도 DB 행은 정상 기록(구버전은 영영 누락)",
      len(_Repo.rows) == before + 1)
check("_watch_scores에 비율(1.2) 저장", abs(s._watch_scores["W1"] - 1.2) < 1e-9)
before = len(_Repo.rows)
s._record_watch_list("W1", "W", 1, {"score": 130, "score_threshold": 100}, "주도주상위")
check("같은 종목 재평가 시 DB 중복 기록 안 함", len(_Repo.rows) == before)
check("재평가 시 점수는 최신값으로 갱신", abs(s._watch_scores["W1"] - 1.3) < 1e-9)

# ═════════════════════════════════════════════════════════
print("\n[13] slot_replacement 중립값 가드")
# ═════════════════════════════════════════════════════════
from core.slot_replacement import find_stagnant_holding, find_replacement_candidate

s = build_strat()
old = datetime(2026, 8, 3, 9, 5, 0) - timedelta(minutes=20)
s.holdings["S1"] = {"buy_price": 10_000, "buy_quantity": 1, "buy_time": old,
                    "stock_name": "S", "highest_price": 10_000, "entry_strength": 150,
                    "entry_score": 1.0}
s._current_strength = lambda code: STRENGTH_NEUTRAL      # 틱 부족 = 판단 불가
check("중립값(100)을 '강도 하락'으로 오판하지 않음",
      find_stagnant_holding(s, s._now()) is None)
s._current_strength = lambda code: 100.0 - 1             # 진짜 하락(<150*0.8)
res = find_stagnant_holding(s, s._now())
check("실제 강도 하락은 정상 감지", res is not None and res[0] == "S1")

s._watch_scores = {"C1": 1.1, "C2": 1.4}
s.watch_list_today = {"C1", "C2"}
cand = find_replacement_candidate(s, 1.0)
check("교체 후보는 1.2배 이상만(1.1 탈락, 1.4 선택)", cand == ("C2", 1.4), str(cand))
cand = find_replacement_candidate(s, 0.0)
check("정체종목 점수 0이어도 문턱 1.2 유지(아무나 통과 금지)",
      cand == ("C2", 1.4), str(cand))

# ═════════════════════════════════════════════════════════
print("\n[14] order_manager 시장가/지정가 분기")
# ═════════════════════════════════════════════════════════
from core.order_manager import OrderManager

class _RestSpy:
    def __init__(self): self.sent = []
    def buy_market_order(self, code, qty, price=0, trde_tp="3"):
        self.sent.append({"price": price, "trde_tp": trde_tp})
        return {"return_code": 0, "ord_no": "X1"}
    def get_current_price(self, code): return 10_000

om = OrderManager(_RestSpy())
om.get_stock_name = lambda c: c
r = om.buy("A", 10, price=0, order_style="market", ref_price=10_050)
check("시장가: trde_tp='3' 이고 주문가 0", om.rest.sent[-1] == {"price": 0, "trde_tp": "3"})
check("시장가: 예상 체결가 = ref_price(매도1호가)", r["price"] == 10_050, str(r))
r = om.buy("A", 10, price=0, order_style="limit")
check("지정가: trde_tp='0' 이고 현재가+1틱", om.rest.sent[-1]["trde_tp"] == "0"
      and om.rest.sent[-1]["price"] > 10_000, str(om.rest.sent[-1]))
r = om.buy("A", 10, price=12_345, order_style="limit")
check("지정가 가격 지정 시 그대로 사용", om.rest.sent[-1]["price"] == 12_345)

class _RestNoPrice(_RestSpy):
    def get_current_price(self, code): return 0
om2 = OrderManager(_RestNoPrice())
om2.get_stock_name = lambda c: c
r = om2.buy("A", 10, order_style="market", ref_price=0)
check("시장가인데 기준가를 못 구하면 주문 안 냄(매수단가 미상 방지)",
      not r["success"] and not om2.rest.sent, str(r))

# ═════════════════════════════════════════════════════════
print("\n[15] 1B 비활성화 확인")
# ═════════════════════════════════════════════════════════
check("PHASE1B_ENABLED = False", SM.PHASE1B_ENABLED is False)
s = build_strat()
s.phase1b.start_watching("B1")
s._try_phase1b_buy("B1")
check("_try_phase1b_buy 호출해도 확증대기 등록 안 됨", not s._1b_confirm)
s._1b_confirm["B1"] = {"ref_high": 1, "since": s._now(), "last_check": s._now()}
s._check_1b_confirmations()
check("_check_1b_confirmations도 아무 동작 안 함", "B1" in s._1b_confirm)
s._1b_confirm.clear()
s.tick()
check("tick()에서도 1B 확증 점검이 돌지 않음", not s._1b_confirm)

# ═════════════════════════════════════════════════════════
print("\n[16] 시장가 거부 시 지정가 폴백 (모의서버 미검증 대비)")
# ═════════════════════════════════════════════════════════
class _RejectMarket(_OrderMgr):
    def buy(self, code, qty, price=0, sizing="REGULAR", exit_strategy="REGULAR",
            order_style="limit", ref_price=0):
        self.orders.append({"code": code, "style": order_style})
        if order_style == "market":
            return {"success": False, "error": "시장가 미지원"}
        return {"success": True, "ord_no": "1", "price": 10_000, "style": "limit"}

s = build_strat()
s.order_manager = _RejectMarket()
s.phase1b.start_watching("F1")
s.phase1b.orderbook.update("F1", {"ask_prices": [10_000, 10_010, 10_020],
                                  "ask_volumes": [3_000, 3_000, 3_000]}, now=now)
feed(s.phase1b.trade_flow, "F1", 5, 1_000_000)
s._execute_buy("F1", "폴백", 1, {"current_price": 10_000}, "1A")
styles = [o["style"] for o in s.order_manager.orders]
check("시장가 거부 -> 지정가로 재시도", styles == ["market", "limit"], str(styles))
check("폴백 후 포지션 정상 등록", "F1" in s.holdings)
check("폴백 시 entry_reason에 limit으로 기록",
      _Repo.rows and _Repo.rows[-1].get("entry_reason", "").endswith("limit"),
      str(_Repo.rows[-1].get("entry_reason", ""))[-40:])

class _RejectAll(_OrderMgr):
    def buy(self, *a, **kw):
        self.orders.append(kw)
        return {"success": False, "error": "잔고부족"}
s = build_strat()
s.order_manager = _RejectAll()
s.phase1b.start_watching("F2")
feed(s.phase1b.trade_flow, "F2", 5, 1_000_000)
s._execute_buy("F2", "실패", 1, {"current_price": 10_000}, "1A")
check("둘 다 실패하면 포지션 등록 안 함", "F2" not in s.holdings)
check("실패해도 pending은 반드시 해제(슬롯 영구점유 방지)", "F2" not in s.pending)

# ═════════════════════════════════════════════════════════
print("\n[17] 통합 — 실시간 편입 이벤트부터 매수까지")
# ═════════════════════════════════════════════════════════
s = build_strat(datetime(2026, 8, 3, 9, 7, 0))
# WS가 파싱해 넘겨주는 값 그대로 (seq=1 -> 주도주상위)
s.on_condition_hit("INT1", "통합종목", is_surge=False, cond_name="주도주상위")
check("편입 즉시 체결틱 감시가 켜짐(1A 평가 진입 확인)",
      s.phase1b.is_watching("INT1"))
check("첫 평가는 틱이 없어 탈락하고 후보로만 남음",
      "INT1" in s.watch_list_today and "INT1" not in s.holdings)

# 대량체결이 터진 뒤 재평가(watchlist_reentry가 15초마다 하는 일)
from core.watchlist_reentry import try_watchlist_reentry
s.phase1b.orderbook.update("INT1", {"ask_prices": [10_000, 10_010, 10_020],
                                    "ask_volumes": [3_000, 3_000, 3_000]}, now=now)
feed(s.phase1b.trade_flow, "INT1", 3, 30_000_000)
bought = try_watchlist_reentry(s, s._now())
check("대량체결 발생 후 재진입 스캔에서 매수 성립", bought == 1 and "INT1" in s.holdings,
      f"bought={bought}")
check("매수 방식이 호가 두께에 따라 시장가로 선택됨",
      s.order_manager.orders[-1]["style"] == "market")
check("sub_strategy가 1A로 기록", s.holdings["INT1"]["sub_strategy"] == "1A")
check("entry_strength 기록(동적캡/슬롯교체 전제조건)",
      s.holdings["INT1"]["entry_strength"] > 0)

# 매수 후에도 틱이 계속 쌓여 동적캡이 살아있는지
s.holdings["INT1"]["warmup_until"] = s._now() - timedelta(seconds=1)
for i in range(6):
    s.on_trade({"stock_code": "INT1", "price": 10_000, "side": "sell", "volume": 5})
check("보유 후에도 강도 갱신됨 -> 동적캡/손실반등 매도가 살아있음",
      s._current_strength("INT1") != STRENGTH_NEUTRAL, str(s._current_strength("INT1")))

# ═════════════════════════════════════════════════════════
print("\n[18] watchlist 재평가 REST 예산 (429 포화 대응)")
# ═════════════════════════════════════════════════════════
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
for i in range(10):
    code = f"Q{i}"
    s.watch_list_today.add(code)
    s._cond_names[code] = "주도주상위"
    s._stock_names[code] = code
    s._opening_prices[code] = 10_000            # 시가 캐시 있음
    s.phase1b.start_watching(code)
    feed(s.phase1b.trade_flow, code, 3, 1_000_000)   # 신선한 체결가(대량체결은 없음)
s.api.calls.clear()
try_watchlist_reentry(s, s._now())
check("1A 후보 10종목 재평가에 REST 분봉 0콜 (구버전은 10콜)",
      len([c for c in s.api.calls if c[0] == "candles"]) == 0, str(s.api.calls))

s2 = build_strat(datetime(2026, 8, 3, 9, 30, 0))
s2.watch_list_today.add("NOOPEN")
s2._cond_names["NOOPEN"] = "주도주상위"
s2._stock_names["NOOPEN"] = "NOOPEN"
s2.phase1b.start_watching("NOOPEN")
feed(s2.phase1b.trade_flow, "NOOPEN", 3, 1_000_000)
s2.api.calls.clear()
try_watchlist_reentry(s2, s2._now())
check("시가 캐시 없으면 REST로 1회 조회(필터를 조용히 끄지 않음)",
      len([c for c in s2.api.calls if c[0] == "candles"]) == 1, str(s2.api.calls))
check("조회한 시가를 캐시 -> 다음 사이클은 REST 0콜",
      s2._opening_prices.get("NOOPEN", 0) > 0, str(s2._opening_prices))
s2.api.calls.clear()
try_watchlist_reentry(s2, s2._now())
check("두 번째 사이클은 실제로 REST 0콜",
      len([c for c in s2.api.calls if c[0] == "candles"]) == 0, str(s2.api.calls))

s3 = build_strat(datetime(2026, 8, 3, 9, 30, 0))
s3.watch_list_today.add("PB1")
s3._cond_names["PB1"] = "눌림목자동"
s3._stock_names["PB1"] = "PB1"
s3._opening_prices["PB1"] = 10_000
s3.phase1b.start_watching("PB1")
feed(s3.phase1b.trade_flow, "PB1", 3, 1_000_000)
s3.api.calls.clear()
try_watchlist_reentry(s3, s3._now())
check("Pullback 후보는 분봉이 필요하므로 REST 조회 유지",
      len([c for c in s3.api.calls if c[0] == "candles"]) >= 1, str(s3.api.calls))

# 고속 경로에서도 매수까지 정상 성립하는지
s4 = build_strat(datetime(2026, 8, 3, 9, 30, 0))
s4.watch_list_today.add("FAST")
s4._cond_names["FAST"] = "주도주상위"
s4._stock_names["FAST"] = "FAST"
s4._opening_prices["FAST"] = 10_000
s4.phase1b.start_watching("FAST")
s4.phase1b.orderbook.update("FAST", {"ask_prices": [10_000, 10_010, 10_020],
                                     "ask_volumes": [3_000, 3_000, 3_000]}, now=now)
feed(s4.phase1b.trade_flow, "FAST", 3, 30_000_000)
s4.api.calls.clear()
n = try_watchlist_reentry(s4, s4._now())
check("REST 0콜 경로로도 1A 매수 성립", n == 1 and "FAST" in s4.holdings, f"n={n}")
check("고속 경로 매수 시에도 분봉 조회 없음",
      len([c for c in s4.api.calls if c[0] == "candles"]) == 0, str(s4.api.calls))

# ═════════════════════════════════════════════════════════
print("\n[19] 시간대별 1A 캡 + 확장 슬롯 도달 가능성")
# ═════════════════════════════════════════════════════════
early = build_strat(datetime(2026, 8, 3, 9, 30, 0))
late = build_strat(datetime(2026, 8, 3, 11, 0, 0))
check("10:30 이전 1A 캡 = 3", early.phase1a_max_slots() == 3)
check("10:30 이후 1A 캡 = 5 (노는 눌림 슬롯 흡수)", late.phase1a_max_slots() == 5)
check("구조상 확장 슬롯 도달 가능해짐 (1A 5 + 눌림 3 = 8 > MAX_HOLDINGS 6)",
      late.phase1a_max_slots() + SM.PULLBACK_MAX_SLOTS > SM.MAX_HOLDINGS)

# ═════════════════════════════════════════════════════════
print("\n[20] pending 오버부킹 방지")
# ═════════════════════════════════════════════════════════
s = build_strat()
for i in range(5):
    s.holdings[f"H{i}"] = {"buy_price": 1, "buy_quantity": 1, "buy_time": s._now(),
                           "stock_name": "H", "highest_price": 1, "sub_strategy": "1A"}
check("보유 5 -> 6번째 매수 가능", s.can_buy_more(None, "1A"))
s.pending.add("PEND1")
s._pending_strategy["PEND1"] = "1A"
check("보유 5 + 매수진행중 1 = 6 -> 더 못 삼 (구버전은 통과)",
      not s.can_buy_more(None, "1A"))
check("occupied_slots가 보유+pending 합산", s.occupied_slots() == 6)
check("전략별 카운트도 pending 포함", s.count_holdings_by_strategy("1A") == 6)
s.pending.discard("PEND1"); s._pending_strategy.pop("PEND1")
check("주문 종료 후 카운트 원복", s.occupied_slots() == 5)

s2 = build_strat()
s2.holdings["X1"] = {"buy_price": 1, "buy_quantity": 1, "buy_time": s2._now(),
                     "stock_name": "X", "highest_price": 1, "sub_strategy": "1A"}
s2.pending.add("X1")   # 매도 진행중
check("매도 진행중(보유∩pending)은 중복 계산 안 함", s2.occupied_slots() == 1)

# ═════════════════════════════════════════════════════════
print("\n[21] candidate_tier — 자기 대비 정규화 (대형/소형 공평)")
# ═════════════════════════════════════════════════════════
s = build_strat()
tfx = s.phase1b.trade_flow
t0 = time.time()
s.phase1b.start_watching("BIG")
s.phase1b.start_watching("SMALL")
# BIG: 절대 거래대금은 압도적이지만 120초 내내 균일 + 매수/매도 반반
for i in range(40):
    tfx.add_tick("BIG", 100_000, "buy" if i % 2 else "sell", 1_000, now=t0 - i * 3)
# SMALL: 절대 규모는 1/10이지만 최근 30초에 몰리고 전부 매수
for i in range(20):
    tfx.add_tick("SMALL", 10_000, "sell", 100, now=t0 - 30 - i * 4.5)   # 과거 90초 잔챙이
for i in range(10):
    tfx.add_tick("SMALL", 10_000, "buy", 1_000, now=t0 - i * 3)         # 최근 30초 집중
tier_big = s.candidate_tier("BIG")
tier_small = s.candidate_tier("SMALL")
check("절대 거래대금은 BIG이 훨씬 큼",
      tfx.get_trade_value("BIG", 120) > tfx.get_trade_value("SMALL", 120) * 5)
check("그래도 tier는 SMALL이 이김 (소형 급등주가 대형주에 안 밀림)",
      tier_small > tier_big, f"BIG={tier_big:.2f} SMALL={tier_small:.2f}")
check("균일·중립 종목의 tier는 1.0 근처", 0.7 < tier_big < 1.5, f"{tier_big:.2f}")
s.phase1b.start_watching("THIN")
tfx.add_tick("THIN", 10_000, "buy", 10_000, now=t0)
check("틱 부족 종목은 tier=0 (판단 불가)", s.candidate_tier("THIN") == 0.0)
check("감시 안 하는 종목도 tier=0", s.candidate_tier("NEVER") == 0.0)
check("가속도: 균일하면 1.0 근처",
      0.8 < tfx.value_acceleration("BIG", 30, 120) < 1.2,
      f"{tfx.value_acceleration('BIG', 30, 120):.2f}")

# ═════════════════════════════════════════════════════════
print("\n[22] 종목 우선순위 — 슬롯 여유 시 '딜레이 0' 보장")
# ═════════════════════════════════════════════════════════
def make_1a_holding(st, code, buy_px, cur_px, age_sec=120, tier_ticks=True):
    st.holdings[code] = {
        "buy_price": buy_px, "buy_quantity": 1, "stock_name": code,
        "buy_time": st._now() - timedelta(seconds=age_sec), "sub_strategy": "1A",
        "highest_price": cur_px, "lowest_price": cur_px, "entry_strength": 150,
        "warmup_until": st._now() - timedelta(seconds=1),
    }
    st.phase1b.start_watching(code)
    if tier_ticks:   # 균일·약한 흐름 -> 낮은 tier
        for i in range(40):
            st.phase1b.trade_flow.add_tick(code, cur_px, "buy" if i % 2 else "sell",
                                           10, now=time.time() - i * 3)

# (a) 슬롯 여유 있음 -> tier 조회 자체를 안 한다
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
tier_calls = []
orig_tier = s.candidate_tier
s.candidate_tier = lambda c: (tier_calls.append(c), orig_tier(c))[1]
s.phase1b.start_watching("FREE")
s.phase1b.orderbook.update("FREE", {"ask_prices": [10_000, 10_010, 10_020],
                                    "ask_volumes": [3_000, 3_000, 3_000]}, now=now)
feed(s.phase1b.trade_flow, "FREE", 3, 30_000_000)
s._cond_names["FREE"] = "주도주상위"
s._evaluate_1a_pullback_entry("FREE", "FREE", 1, None, 10_000, 9_800,
                              datetime(2026, 8, 3, 9, 30, 0).time())
check("슬롯 여유 시 매수 성립", "FREE" in s.holdings)
check("슬롯 여유 시 우선순위(tier) 계산을 아예 하지 않음 = 진입 지연 0",
      tier_calls == ["FREE"], f"tier 호출: {tier_calls}")
# ↑ entry_tier 기록용 1회만 (매수 확정 후), 판정용 호출은 0회

# (b) 슬롯 만석 + 보유가 정체 상태 -> 교체
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
for i in range(3):
    make_1a_holding(s, f"W{i}", 10_000, 10_000)     # 전부 flat(정체)
s.phase1b.start_watching("HOT")
for i in range(20):
    s.phase1b.trade_flow.add_tick("HOT", 10_000, "sell", 10, now=time.time() - 30 - i * 4.5)
for i in range(10):
    s.phase1b.trade_flow.add_tick("HOT", 10_000, "buy", 3_000, now=time.time() - i * 3)
before = len(s.holdings)
swapped = s._try_1a_priority_upgrade("HOT", s.candidate_tier("HOT"))
check("만석 + 정체 보유 -> 더 강한 후보로 교체 성립", swapped and len(s.holdings) == before - 1,
      f"swapped={swapped}, 보유 {before}->{len(s.holdings)}")

# (c) 보유가 이미 오르고 있으면 안 건드림
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
for i in range(3):
    make_1a_holding(s, f"U{i}", 10_000, 10_300)     # +3% 상승 중
    for j in range(6):   # 최신 체결가 = 10,300
        s.phase1b.trade_flow.add_tick(f"U{i}", 10_300, "buy", 10, now=time.time() - j)
s.phase1b.start_watching("HOT2")
for i in range(20):
    s.phase1b.trade_flow.add_tick("HOT2", 10_000, "sell", 10, now=time.time() - 30 - i * 4.5)
for i in range(10):
    s.phase1b.trade_flow.add_tick("HOT2", 10_000, "buy", 3_000, now=time.time() - i * 3)
swapped = s._try_1a_priority_upgrade("HOT2", s.candidate_tier("HOT2"))
check("수익 중인 포지션은 tier가 낮아도 절대 교체 대상 아님", not swapped)

# (d) tier 판단 불가 후보는 남의 슬롯을 빼앗지 못함
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
for i in range(3):
    make_1a_holding(s, f"V{i}", 10_000, 10_000)
check("tier=0(판단 불가) 후보는 교체 시도 안 함",
      not s._try_1a_priority_upgrade("UNKNOWN", 0.0))

# (e) 최소 보유시간 미달 포지션 보호
s = build_strat(datetime(2026, 8, 3, 9, 30, 0))
make_1a_holding(s, "NEW", 10_000, 10_000, age_sec=5)
check("매수 5초 지난 포지션은 교체 대상에서 제외(수수료 낭비 방지)",
      not s._try_1a_priority_upgrade("HOT3", 99.0))

# ═════════════════════════════════════════════════════════
print("\n[23] 정체 포지션 15분 조기 정리")
# ═════════════════════════════════════════════════════════
def dead_pos(st, code, held_min, net_pct):
    px = 10_000
    cur = int(px * (1 + net_pct / 100 + SM.ROUND_TRIP_COST))
    st.holdings[code] = {
        "trade_id": 1, "buy_price": px, "buy_quantity": 1, "stock_name": code,
        "buy_time": st._now() - timedelta(minutes=held_min), "sub_strategy": "1A",
        "highest_price": cur, "lowest_price": cur,
        "warmup_until": st._now() - timedelta(seconds=1), "entry_strength": 0,
    }
    st.on_price_update(code, cur)
    return code in st.holdings

s = build_strat()
check("15분 경과 & ±0.5% 이내 -> 정체 정리로 청산", not dead_pos(s, "D1", 16, 0.1))
check("청산 사유가 '정체 정리'로 DB에 기록",
      any("정체 정리" in str(r.get("exit_reason", "")) for r in _Repo.sells),
      str([r.get("exit_reason") for r in _Repo.sells]))
s = build_strat()
check("15분 경과했지만 +1.2% -> 유지(익절 캡이 담당)", dead_pos(s, "D2", 16, 1.2))
s = build_strat()
check("15분 경과했지만 -1.5% -> 유지(손절이 담당)", dead_pos(s, "D3", 16, -1.5))
s = build_strat()
check("10분밖에 안 됐으면 정체여도 유지", dead_pos(s, "D4", 10, 0.1))
s = build_strat()
check("30분 초과는 기존 시간정리로 청산", not dead_pos(s, "D5", 31, 1.0))

# ═════════════════════════════════════════════════════════
print("\n[24] 제안 A — 신호 세기 계층화 (단일 대량체결 우대)")
# ═════════════════════════════════════════════════════════
def tier_with(single_value, n=12):
    st = build_strat()
    st.phase1b.start_watching("S")
    tfl = st.phase1b.trade_flow
    t = time.time()
    for i in range(n):                       # 과거 90초 잔챙이
        tfl.add_tick("S", 10_000, "buy", 10, now=t - 30 - i * 7)
    for i in range(n):                       # 최근 30초
        vol = int(single_value // 10_000) if i == 0 else 100
        tfl.add_tick("S", 10_000, "buy", vol, now=t - i * 2)
    tfx2 = st.phase1b.trade_flow
    accel = tfx2.value_acceleration("S", 30, 120)
    stg = tfx2.compute_strength("S", 30, min_ticks=10)
    return st.candidate_tier("S"), accel * stg / 100.0

tier_plain, base_plain = tier_with(1_000_000)      # 최대 단일 100만
tier_burst, base_burst = tier_with(30_000_000)     # 최대 단일 3천만
tier_big, base_big = tier_with(100_000_000)        # 최대 단일 1억
check("단일체결 작으면 가중 없음 (x1.0)",
      abs(tier_plain - base_plain) < 1e-6, f"{tier_plain:.3f} vs {base_plain:.3f}")
check("3천만+ 단일체결 -> x1.2 가중",
      abs(tier_burst - base_burst * SM.PHASE1A_TIER_BURST_MULT) < 1e-6,
      f"{tier_burst:.3f} vs {base_burst * 1.2:.3f}")
check("1억+ 단일체결 -> x1.5 가중",
      abs(tier_big - base_big * SM.PHASE1A_TIER_SINGLE_MULT) < 1e-6,
      f"{tier_big:.3f} vs {base_big * 1.5:.3f}")
check("가중 배수는 상한 고정 (tier 폭주 없음)",
      SM.PHASE1A_TIER_SINGLE_MULT <= 1.5 and SM.PHASE1A_TIER_BURST_MULT <= 1.5)

# ═════════════════════════════════════════════════════════
print("\n[25] 제안 B — 조건검색식별 성과 자동 보정")
# ═════════════════════════════════════════════════════════
check("병합 조건명도 대표 키 하나로 접힘",
      SM.StrategyManager.cond_perf_key("주도주상위+돌파자동매매용") == "cond:주도주상위")
check("돌파자동매매용 단독 키", SM.StrategyManager.cond_perf_key("돌파자동매매용") == "cond:돌파자동매매용")
check("알 수 없는 조건명은 기타로", SM.StrategyManager.cond_perf_key("기타") == "cond:기타")

def thr_for(cond, losses=None):
    st = build_strat(datetime(2026, 8, 3, 10, 0, 0))   # ACTIVE_FROM(09:20) 이후
    if losses:
        for v in losses:
            st.perf.record(SM.StrategyManager.cond_perf_key(cond), v)
    st.phase1b.start_watching("T")
    feed(st.phase1b.trade_flow, "T", 3, 30_000_000)
    ok, info = st.evaluate_1a_leading_strength("T", 10_000, 0.0, cond)
    return info.get("strength_threshold", 0)

base_thr = thr_for("주도주상위")
cold_thr = thr_for("주도주상위", [-0.02, -0.025, -0.02, -0.03])
check("조건식 성과가 나쁘면 그 검색식 종목의 강도 문턱이 올라감",
      cold_thr > base_thr, f"{base_thr:.1f} -> {cold_thr:.1f}")
check("조정폭은 상한(±15%) 내로 제한",
      cold_thr <= base_thr * 1.16, f"{cold_thr:.1f}")
hot_thr = thr_for("주도주상위", [0.02, 0.025, 0.02, 0.03])
check("성과가 좋아도 100 밑으로는 절대 안 내려감(바닥 고정)",
      hot_thr >= SM.PHASE1A_LEADING_STRENGTH_MIN, f"{hot_thr:.1f}")
check("표본 부족(2건)이면 조정 안 함",
      thr_for("주도주상위", [-0.03, -0.03]) == base_thr)

s = build_strat()
s.phase1b.start_watching("CK")
s.phase1b.orderbook.update("CK", {"ask_prices": [10_000], "ask_volumes": [10]}, now=now)
feed(s.phase1b.trade_flow, "CK", 5, 1_000_000)
s._cond_names["CK"] = "돌파자동매매용"
s._execute_buy("CK", "CK", 1, {"current_price": 10_000}, "1A")
check("매수 시 조건식 성과 키가 포지션에 기록",
      s.holdings["CK"].get("cond_key") == "cond:돌파자동매매용")
s._execute_sell("CK", 10_500, "익절 테스트")
check("청산 시 조건식 축에도 성과가 쌓임",
      s.perf.sample_count("cond:돌파자동매매용") == 1)

# ═════════════════════════════════════════════════════════
print("\n[26] 제안 C — tier 기반 매수금액 가중 (상방 한정)")
# ═════════════════════════════════════════════════════════
M = SM.StrategyManager.tier_size_multiplier
check("tier 1.0 -> 1.0배 (기존과 동일)", M(1.0) == 1.0)
check("tier 0.3 -> 1.0배 (아래로는 안 줄임)", M(0.3) == 1.0)
check("tier 1.25 -> 1.25배 (선형)", abs(M(1.25) - 1.25) < 1e-9, str(M(1.25)))
check("tier 1.5 -> 1.5배 (상한 도달)", abs(M(1.5) - 1.5) < 1e-9)
check("tier 50 -> 1.5배 (상한 고정, 폭주 없음)", abs(M(50.0) - 1.5) < 1e-9)
check("tier None/이상값 -> 1.0배 (안전)", M(None) == 1.0 and M("x") == 1.0)

s = build_strat()
amt_plain, _ = s._resolve_position_amount("A", "1A", tier=1.0)
amt_boost, _ = s._resolve_position_amount("A", "1A", tier=1.5)
check("금액이 tier에 따라 확대됨",
      amt_boost == int(amt_plain * 1.5), f"{amt_plain:,} -> {amt_boost:,}")
check("확대 상한이 기본금액의 1.5배를 넘지 않음",
      amt_boost <= SM.POSITION_AMOUNT * 1.5)

s = build_strat()
s.phase1b.start_watching("SZ")
tfz = s.phase1b.trade_flow
t = time.time()
for i in range(12):
    tfz.add_tick("SZ", 10_000, "buy", 10, now=t - 30 - i * 7)
for i in range(12):
    tfz.add_tick("SZ", 10_000, "buy", 10_000 if i == 0 else 100, now=t - i * 2)
s.phase1b.orderbook.update("SZ", {"ask_prices": [10_000], "ask_volumes": [10]}, now=now)
s._execute_buy("SZ", "SZ", 1, {"current_price": 10_000}, "1A")
qty = s.holdings["SZ"]["buy_quantity"]
check("강한 신호 종목은 실제 매수 수량도 늘어남",
      qty > SM.POSITION_AMOUNT // 10_000, f"{qty}주 (기본 {SM.POSITION_AMOUNT // 10_000}주)")
check("entry_tier가 포지션에 기록(사후 검증용)", s.holdings["SZ"].get("entry_tier", 0) > 0)

# ═════════════════════════════════════════════════════════
print("\n[27] 진단 알림 — '포착은 되는데 왜 안 사는가'")
# ═════════════════════════════════════════════════════════
C = SM.StrategyManager._reject_category
check("사유 분류: 대량체결", C("대량체결 부족 (최근 3초: ...)") == "대량체결 부족")
check("사유 분류: 체결틱", C("체결틱 부족 (최근 3초 1틱 < 최소 3틱)") == "체결틱 부족(강도판단불가)")
check("사유 분류: 강도", C("체결강도 미달 (80 < 100)") == "체결강도 미달")
check("사유 분류: 슬롯", C("슬롯 부족 (1A 3/3, 전체 6/6)") == "슬롯 부족")
check("사유 분류: 재매수", C("손절 종목 당일 재매수 금지") == "재매수 차단")
check("사유 분류: 미지정은 기타", C("알 수 없는 무언가") == "기타")

s = build_strat(datetime(2026, 8, 3, 10, 0, 0))
for i in range(4):
    code = f"R{i}"
    s.watch_list_today.add(code)
    s.phase1b.start_watching(code)
    feed(s.phase1b.trade_flow, code, 5, 1_000_000)
    s._note_reject(code, "대량체결 부족 (최근 3초: 3,000만원+ 체결 0건/3건)")
s._note_reject("R0", "체결틱 부족 (최근 3초 1틱 < 최소 3틱)")
msg = s.build_entry_diagnostics()
check("진단문에 후보 수/보유 현황 포함", "후보 4종목" in msg and "보유 0/6" in msg, msg[:60])
check("사유별 집계가 표시됨", "대량체결 부족" in msg and "미체결 사유" in msg)
check("인프라 의심 사유는 경고로 승격", "체결 데이터 없음" in msg, msg)
check("슬롯 여유 상태가 표시됨", "1A 슬롯 여유" in msg)
check("오늘 매수 0건 경고", "매수 0건" in msg, msg)

s2 = build_strat(datetime(2026, 8, 3, 10, 0, 0))
s2.watch_list_today.add("SILENT")
s2.phase1b.start_watching("SILENT")      # 감시중인데 틱 0
s2._note_reject("SILENT", "체결강도 미달 (0 < 100)")
msg2 = s2.build_entry_diagnostics()
check("감시중인데 체결틱 0인 종목을 지목", "체결틱 0인 종목 1개" in msg2, msg2)

s3 = build_strat(datetime(2026, 8, 3, 10, 0, 0))
s3.watch_list_today.add("NOWATCH")       # 후보인데 감시조차 미시작
s3._note_reject("NOWATCH", "조건식 지연 — 돌파자동매매용는 09:20부터 평가")
msg3 = s3.build_entry_diagnostics()
check("후보인데 감시 미시작인 종목을 지목(1A 평가 미도달)",
      "감시 미시작 1개" in msg3, msg3)

s4 = build_strat(datetime(2026, 8, 3, 10, 0, 0))
for i in range(6):
    s4.holdings[f"F{i}"] = {"buy_price": 1, "buy_quantity": 1, "buy_time": s4._now(),
                            "stock_name": "F", "highest_price": 1, "sub_strategy": "1A"}
s4.watch_list_today.add("WAIT")
s4._note_reject("WAIT", "슬롯 부족 (1A 3/3, 전체 6/6)")
msg4 = s4.build_entry_diagnostics()
check("슬롯 만석이면 '슬롯 없음'으로 안내(오작동 아님을 구분)",
      "슬롯 없음" in msg4, msg4)

s5 = build_strat(datetime(2026, 8, 3, 10, 0, 0))
s5.quarantine_until = s5._now() + timedelta(minutes=5)
check("WS 재연결 격리 중이면 전면 차단 사유로 표시",
      "WS 재연결 격리" in s5.build_entry_diagnostics())

s6 = build_strat(datetime(2026, 8, 3, 10, 0, 0))
s6.watch_list_today.add("OLD")
s6._last_reject["OLD"] = ("대량체결 부족", "x", s6._now() - timedelta(minutes=30))
check("오래된(10분 초과) 평가 기록은 집계에서 제외 -> 재평가 정지 경고",
      "재평가 루프 정지 의심" in s6.build_entry_diagnostics())
check("진단 생성 중 예외 없음(어떤 상태에서도 문자열 반환)",
      isinstance(build_strat().build_entry_diagnostics(), str))

# ═════════════════════════════════════════════════════════
print("\n[28] 전략별 등락률 상한 + 눌림목 전용 라우팅 하드가드")
# ═════════════════════════════════════════════════════════
CAP = SM.StrategyManager._entry_change_cap
check("1A 상한 16%", CAP("1A", "주도주상위") == 16.0)
check("돌파자동매매용도 16%", CAP("1A", "돌파자동매매용") == 16.0)
check("눌림목 상한 10% (전략 기준)", CAP("1A_눌림", "") == 10.0)
check("눌림목 상한 10% (조건명 기준)", CAP("1A", "눌림목자동") == 10.0)
check("병합 조건명에 눌림목자동 있으면 보수적으로 10%",
      CAP("1A", "주도주상위+눌림목자동") == 10.0)

def buy_at(change_pct, sub, cond):
    """전일종가 대비 change_pct 상태에서 매수가 성립하는지."""
    st = build_strat()
    st.api.get_stock_change_rate = lambda c: change_pct
    st.phase1b.start_watching("E")
    st.phase1b.orderbook.update("E", {"ask_prices": [10_000], "ask_volumes": [10]}, now=now)
    feed(st.phase1b.trade_flow, "E", 5, 1_000_000)
    st._cond_names["E"] = cond
    st._execute_buy("E", "E", 1, {"current_price": 10_000}, sub)
    return "E" in st.holdings

check("1A: +14% 통과 (구버전 12% 상한이면 차단됐음)", buy_at(14.0, "1A", "주도주상위"))
check("1A: +17% 차단", not buy_at(17.0, "1A", "주도주상위"))
check("눌림목: +8% 통과", buy_at(8.0, "1A_눌림", "눌림목자동"))
check("눌림목: +12% 차단 (1A였다면 통과했을 값)", not buy_at(12.0, "1A_눌림", "눌림목자동"))

s = build_strat()
s.phase1b.start_watching("PBONLY")
s.phase1b.orderbook.update("PBONLY", {"ask_prices": [10_000], "ask_volumes": [10]}, now=now)
feed(s.phase1b.trade_flow, "PBONLY", 5, 1_000_000)
s._cond_names["PBONLY"] = "눌림목자동"
s._execute_buy("PBONLY", "PBONLY", 1, {"current_price": 10_000}, "1A")
check("눌림목자동 종목을 1A로 매수 시도 -> 주문 직전 하드가드가 차단",
      "PBONLY" not in s.holdings and not s.order_manager.orders,
      str(s.order_manager.orders))
s._execute_buy("PBONLY", "PBONLY", 1, {"current_price": 10_000}, "1A_눌림")
check("같은 종목을 Pullback으로 매수하면 정상 통과", "PBONLY" in s.holdings)

s2 = build_strat()
s2.phase1b.start_watching("MERGED")
s2.phase1b.orderbook.update("MERGED", {"ask_prices": [10_000], "ask_volumes": [10]}, now=now)
feed(s2.phase1b.trade_flow, "MERGED", 5, 1_000_000)
s2._cond_names["MERGED"] = "주도주상위+눌림목자동"   # 나중에 병합된 경우
s2._execute_buy("MERGED", "MERGED", 1, {"current_price": 10_000}, "1A")
check("병합 조건명이어도 눌림목자동이 포함되면 1A 매수 차단",
      "MERGED" not in s2.holdings)

s3 = build_strat(datetime(2026, 8, 3, 9, 30, 0))
calls = []
s3.evaluate_1a_leading_strength = lambda *a, **k: (calls.append("1A"), (False, {"reason": "x"}))[1]
s3.evaluate_pullback = lambda *a, **k: (calls.append("PB"), (False, {"reason": "x"}))[1]
s3._cond_names["RT"] = "주도주상위+눌림목자동"
s3._evaluate_1a_pullback_entry("RT", "RT", 1, make_candles(15), 10_000, 9_900,
                               datetime(2026, 8, 3, 9, 30, 0).time())
check("라우팅 단계에서도 눌림목자동 포함이면 Pullback만 평가", calls == ["PB"], str(calls))

# ═════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print("  -", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
