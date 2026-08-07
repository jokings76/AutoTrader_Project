"""08-06 실거래 재생 회귀 — "어제의 잘못된 매매가 다시 일어나지 않는가".

08-06은 개장 18분에 **매수 19건 / 손절 7건**이 나와 09:19에 수동 정지한 날이다.
그 뒤 [A]~[H] 8건과 무장 3초 복원이 들어갔는데, 기존 스위트는 각 항목을
**개별 사양**으로만 검증한다("이 게이트가 이 입력을 막는가").

이 파일이 보는 것은 다르다 — **그날의 실제 19건을 실제 코드에 다시 태워서**
   ① 손실 건이 지금은 막히는가
   ② **승자 4건은 그대로 살아남는가**  <- 이게 더 중요하다
를 같이 확인한다. 게이트를 조이는 변경은 손실만 막는 게 아니라 이익도 같이
막을 수 있고, 그건 개별 사양 테스트로는 절대 드러나지 않는다.

데이터 출처: DB `trades` 테이블의 08-06 실체결 19건. 무장 지속시간과 버스트
경로/규모는 `entry_reason`에 그대로 남아 있다(로그 역매칭이 아니라 DB 원본).

⚠️ 재현할 수 없는 것 — 정직하게 적어둔다:
  · **틱 아카이브가 없다.** 09:20 태스크가 09:19:01 정지로 20초 차이로 실행되지
    못했다. 그래서 그날의 실제 틱 흐름(파동 순번 [H], 버스트 방향 [B])은
    원리적으로 재현 불가다. 이 파일은 그 두 항목을 **합성 틱**으로 검증한다.
  · **편입 시각이 DB에 없다.** [F] 숙성은 편입 시각 기준인데 그 값이 없어,
    "09:00 편입 가정"으로 계산한 결과를 참고로만 싣는다(단언하지 않는다).

실행: python test_replay_20260806.py   (종료코드 0 = 전원 통과)
"""
import sys
import time
from datetime import datetime, timedelta as _td

import os as _os_testlog
# 실거래 로그(autotrader.log) 오염 방지 — 반드시 core/main 임포트보다 먼저.
_os_testlog.environ["AUTOTRADER_TEST_LOG"] = "1"

import core.strategy_manager as SM
from core.phase1b_controller import Phase1BController

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


# ─────────────────────────────────────────────────────────
# 스텁 — test_patch_20260804/05/06과 **동일 계약**.
# ⚠️ 감사 파일이 경고하는 함정: 스텁이 실물과 다른 형식을 돌려주면
#    감사 자체가 거짓말을 한다(08-05 심야에 __getattr__ None 스텁으로
#    "매도가 하나도 안 나가는" 가짜 통과를 만든 적이 있다).
# ─────────────────────────────────────────────────────────
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
    def update(cls, row_id, data): cls.updates.append({"id": row_id, **data}); return True
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
    def is_leading_theme_stock(self, code): return False


class _Rest:
    host = "https://mock"

    def __init__(self, change_rate=3.0):
        self.calls = []
        self.change_rate = change_rate
        self.candles = None

    def get_minute_candles(self, code, interval=1, count=1, base_date=None):
        self.calls.append(("candles", code, count))
        if self.candles is not None:
            return list(self.candles)
        return [{"time_str": "20260806090000", "open": 9_990, "high": 10_010,
                 "low": 9_980, "close": 10_000, "volume": 1_000}] * max(1, count)

    def get_orderable_amount(self): return 10_000_000
    def get_stock_change_rate(self, code): return self.change_rate
    def get_basic_quote(self, code): return {"change_rate": self.change_rate}
    def get_index_change_rate(self, s="001"): return 0.0
    def get_current_price(self, code): return 10_000


class _OrderMgr:
    def __init__(self): self.orders = []

    def buy(self, code, qty, price=0, sizing="REGULAR", exit_strategy="REGULAR",
            order_style="limit", ref_price=0):
        self.orders.append({"code": code, "qty": qty, "style": order_style,
                            "ref_price": ref_price, "side": "buy"})
        return {"success": True, "ord_no": "1", "price": ref_price or 10_000,
                "style": order_style}

    def sell(self, code, qty, price=0, order_style="market"):
        self.orders.append({"code": code, "qty": qty, "style": order_style,
                            "side": "sell"})
        return {"success": True, "ord_no": "2", "price": price, "style": order_style}

    def get_stock_name(self, code): return code


def build(now_dt=datetime(2026, 8, 6, 9, 30, 0), change_rate=3.0):
    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows, _Repo.sells, _Repo.updates = [], [], []
    return SM.StrategyManager(
        kiwoom_rest=_Rest(change_rate), order_manager=_OrderMgr(),
        phase1b_controller=Phase1BController(), portfolio_optimizer=None,
        now_func=lambda: now_dt,
    )


def setup(strat, code, cond="주도주상위", ask=10_000, open_px=10_000, ripe=True):
    if ripe:
        strat._first_seen[code] = time.time() - 999
    strat._cond_names[code] = cond
    strat._stock_names[code] = code
    strat.watch_list_today.add(code)
    if open_px:
        strat._opening_prices[code] = open_px
    strat.phase1b.start_watching(code)
    strat.phase1b.orderbook.update(
        code, {"ask_prices": [ask, ask + 10, ask + 20],
               "ask_volumes": [3_000, 3_000, 3_000]}, now=time.time())


def feed(tf, code, n, value_each, now, span=0.5, side="buy", price=10_000):
    vol = max(1, int(value_each // price))
    for i in range(n):
        tf.add_tick(code, price, side, vol, now=now - span * (i / max(1, n)))


# ═════════════════════════════════════════════════════════
# 08-06 실체결 19건 (DB trades 원본)
#   buy_time, code, name, buy_price, 무장초, 버스트경로, 실현손익%, 청산사유
#   change_pct = 전일종가 대비 (진입 시점, ka10001 소급 복원값)
#   open_pct   = 당일시가 대비 (진입 시점)
# ═════════════════════════════════════════════════════════
TRADES = [
    # (시각,       코드,     이름,        매수가,  무장초, 경로,   손익%,  전일대비, 시가대비)
    ("09:00:31", "006340", "대원전선",    15_580,  2.1, "단일", -3.02,  +4.15,  +2.10),
    ("09:00:36", "006360", "GS건설",      32_300, 19.8, "절대", +0.46,  +3.52,  +1.10),
    ("09:01:23", "024840", "KBI메탈",      5_360,  8.6, "절대", -3.17,  +6.90,  +3.20),
    ("09:01:28", "439960", "코스모로보틱스", 20_500,  9.9, "절대", +3.17,  +7.90,  +4.10),
    ("09:01:36", "073240", "금호타이어",    7_380, 17.2, "절대", +2.85,  +0.25,  +2.50),
    ("09:01:55", "036930", "주성엔지니어링", 141_100, 8.4, "단일",  0.00,  -1.26,  +1.80),
    ("09:03:50", "069540", "빛과전자",     2_895,  3.2, "절대",  0.00,  +5.10,  +2.90),
    ("09:05:09", "058610", "에스피지",   117_800,  1.6, "절대", -3.06,  +8.30,  +3.60),
    ("09:05:55", "006360", "GS건설",      32_600, 12.0, "절대", -3.07,  +4.48,  +2.05),
    ("09:06:08", "439960", "코스모로보틱스", 20_875, 91.3, "절대",  0.00,  +9.85,  +6.00),
    ("09:07:22", "222800", "심텍",       113_200, 51.6, "절대",  0.00,  +5.40,  +3.10),
    ("09:07:39", "036930", "주성엔지니어링", 140_100, 105.2, "절대", 0.00, -1.96,  +1.10),
    ("09:08:15", "439960", "코스모로보틱스", 20_600, 30.9, "절대", -3.01,  +8.40,  +4.60),
    ("09:08:21", "067290", "JW신약",       2_070, 47.6, "절대",  0.00,  +4.20,  +2.30),
    ("09:10:13", "004310", "현대약품",     6_120, 81.1, "절대", +4.25,  +4.61,  +3.30),
    ("09:10:51", "069540", "빛과전자",     3_113,  6.0, "절대", -3.15,  +9.90,  +7.40),
    ("09:11:55", "222800", "심텍",       113_900, 78.4, "단일", -3.07,  +6.10,  +3.80),
    ("09:15:34", "058730", "다스코",       3_305, 85.3, "단일",  0.00,  +7.20,  +4.90),
    ("09:18:21", "073240", "금호타이어",    8_210, 22.3, "절대",  0.00, +11.50, +14.03),
]

WINNERS = {("09:00:36", "006360"), ("09:01:28", "439960"),
           ("09:01:36", "073240"), ("09:10:13", "004310")}
LOSERS = {t[0] + t[1] for t in TRADES if t[6] <= -3.0}

print("=" * 66)
print("08-06 실거래 19건 재생 — 현행 코드에서 각 건이 어떻게 되는가")
print("=" * 66)

# ═════════════════════════════════════════════════════════
print("\n[1] 무장 3.0초 — 그날 통과한 건 중 지금 막히는 것")
# ═════════════════════════════════════════════════════════
s = build(datetime(2026, 8, 6, 9, 30, 0))
blocked_arm, passed_arm = [], []
for t, code, name, px, sus, path, pnl, chg, opn in TRADES:
    (passed_arm if sus >= SM.TICK_STRENGTH_SUSTAIN_SEC else blocked_arm).append(
        (t, name, sus, pnl))

check("무장 상수가 3.0초", SM.TICK_STRENGTH_SUSTAIN_SEC == 3.0,
      f"{SM.TICK_STRENGTH_SUSTAIN_SEC}")
check("무장 3초로 막히는 건이 정확히 2건", len(blocked_arm) == 2,
      ", ".join(f"{n}({s_}초 {p:+.2f}%)" for _, n, s_, p in blocked_arm))
check("막힌 2건이 전부 손절", all(p <= -3.0 for *_, p in blocked_arm),
      f"합계 {sum(p for *_, p in blocked_arm):+.2f}%")
check("승자 4건은 무장 3초를 전부 통과",
      all(sus >= 3.0 for t, c, n, px, sus, pa, pnl, ch, op in TRADES
          if (t, c) in WINNERS),
      ", ".join(f"{n} {sus}초" for t, c, n, px, sus, pa, pnl, ch, op in TRADES
                if (t, c) in WINNERS))

# 실제 코드로 확인 — 상수만 비교하면 게이트가 죽어도 통과한다.
s1 = build(datetime(2026, 8, 6, 9, 5, 9))
setup(s1, "058610", open_px=113_700)
now1 = time.time()
s1._strength_since["058610"] = now1 - 1.6      # 에스피지 실측 1.6초
feed(s1.phase1b.trade_flow, "058610", 2,
     SM.PHASE1A_BURST_TRADE_VALUE * SM.burst_price_scale(117_800) * 1.2,
     now=now1, price=117_800)
ok1, info1 = s1.evaluate_tick_entry("058610", "1A", 117_800,
                                    open_price=113_700, now=now1)
check("[실코드] 에스피지 1.6초 -> evaluate_tick_entry 탈락", not ok1,
      info1.get("reason", "")[:52])

s2 = build(datetime(2026, 8, 6, 9, 10, 13))
setup(s2, "004310", open_px=5_925)
now2 = time.time()
s2._strength_since["004310"] = now2 - 81.1     # 현대약품 실측 81.1초
feed(s2.phase1b.trade_flow, "004310", 2,
     SM.PHASE1A_BURST_TRADE_VALUE * SM.burst_price_scale(6_120) * 1.2,
     now=now2, price=6_120)
ok2, info2 = s2.evaluate_tick_entry("004310", "1A", 6_120,
                                    open_price=5_925, now=now2)
check("[실코드·대조군] 현대약품 81.1초 -> 통과(승자 보존)", ok2,
      info2.get("reason", "")[:52])

# ═════════════════════════════════════════════════════════
print("\n[2] [C] 등락률 하한 — 전일종가 대비 마이너스 매수 차단")
# ═════════════════════════════════════════════════════════
s3 = build(datetime(2026, 8, 6, 9, 1, 55), change_rate=-1.26)
setup(s3, "036930", open_px=138_600)
r3 = s3._entry_change_reject("036930", "1A", 141_100)
check("[실코드] 주성 09:01:55(전일 -1.26%) 차단", bool(r3), str(r3)[:52])

s4 = build(datetime(2026, 8, 6, 9, 7, 39), change_rate=-1.96)
setup(s4, "036930", open_px=138_600)
r4 = s4._entry_change_reject("036930", "1A", 140_100)
check("[실코드] 주성 09:07:39(전일 -1.96%) 차단", bool(r4), str(r4)[:52])

neg = [t for t in TRADES if t[7] <= 0]
check("전일 대비 마이너스 매수가 정확히 2건(둘 다 주성)", len(neg) == 2,
      ", ".join(f"{t[2]} {t[7]:+.2f}%" for t in neg))

# 승자 4건이 하한을 넘는가 — 금호타이어가 +0.25%로 아슬아슬하다.
for t, code, name, px, sus, path, pnl, chg, opn in TRADES:
    if (t, code) in WINNERS:
        sw = build(datetime(2026, 8, 6, 9, 30, 0), change_rate=chg)
        setup(sw, code, open_px=int(px / (1 + opn / 100)))
        rw = sw._entry_change_reject(code, "1A", px)
        check(f"[실코드·승자] {name} {chg:+.2f}% 하한 통과", rw is None,
              str(rw or "통과")[:44])

# ═════════════════════════════════════════════════════════
print("\n[3] [A] 시가대비 — 금호타이어 09:18 +14.03% 차단 (그날 뚫린 건)")
# ═════════════════════════════════════════════════════════
s5 = build(datetime(2026, 8, 6, 9, 18, 21), change_rate=11.50)
setup(s5, "073240", open_px=7_200)            # 진짜 시가(ka10001 확정)
now5 = time.time()
s5._strength_since["073240"] = now5 - 22.3
feed(s5.phase1b.trade_flow, "073240", 2,
     SM.PHASE1A_BURST_TRADE_VALUE * SM.burst_price_scale(8_210) * 1.5,
     now=now5, price=8_210)
ok5, info5 = s5.evaluate_tick_entry("073240", "1A", 8_210,
                                    open_price=7_200, now=now5)
surge = (8_210 - 7_200) / 7_200 * 100
check(f"[실코드] 금호타이어 시가대비 +{surge:.2f}% -> 차단", not ok5,
      info5.get("reason", "")[:52])

# A-1: 창 절단 가드가 09:03봉을 시가로 착각하지 않는가 (그날의 근본 원인)
s6 = build(datetime(2026, 8, 6, 9, 18, 21))
truncated = [{"time_str": f"2026080609{m:02d}00", "open": 7_650, "high": 7_700,
              "low": 7_600, "close": 7_650, "volume": 100}
             for m in range(18, 2, -1)]        # 09:18 -> 09:03 (09:00봉 없음)
check("[실코드] A-1 절단 창 -> 0.0(판단 포기)", s6._today_open(truncated) == 0.0,
      f"{s6._today_open(truncated)}")

full = [{"time_str": "20260806090300", "open": 7_650, "high": 7_700,
         "low": 7_600, "close": 7_650, "volume": 100},
        {"time_str": "20260806090000", "open": 7_200, "high": 7_400,
         "low": 7_150, "close": 7_380, "volume": 900}]
check("[실코드·대조군] 09:00봉이 있으면 7,200 정상 반환",
      s6._today_open(full) == 7_200, f"{s6._today_open(full)}")

# A-2: 캐시가 분봉 추정을 이긴다
s7 = build(datetime(2026, 8, 6, 9, 18, 21))
setup(s7, "073240", open_px=7_200)
resolved = s7._opening_prices.get("073240", 0.0) or 7_650
check("[실코드] A-2 캐시(7,200)가 분봉 추정(7,650)을 이긴다", resolved == 7_200,
      f"{resolved}")

# ═════════════════════════════════════════════════════════
print("\n[4] [D] 상승 이탈 OFF — 추격매수가 되살아나지 않는가")
# ═════════════════════════════════════════════════════════
check("ENTRY_BREAKOUT_ENABLED == False", SM.ENTRY_BREAKOUT_ENABLED is False)
check("폭 상수는 보존(되살릴 때 재사용)", SM.ENTRY_BREAKOUT_PCT == 0.003,
      f"{SM.ENTRY_BREAKOUT_PCT}")

s8 = build(datetime(2026, 8, 6, 9, 30, 0))
setup(s8, "TB", open_px=10_000)
now8 = time.time()
s8._entry_plans["TB"] = {
    "trigger_price": 10_000,
    "targets": [{"price": 9_970, "frac": 0.5, "depth": 0.003, "filled": False},
                {"price": 9_930, "frac": 0.5, "depth": 0.007, "filled": False}],
    "deadline": now8 + 120, "sub_strategy": "1A",
    "info": {"current_price": 10_000}, "cond_name": "주도주상위",
    "phase": 1, "stock_name": "TB",
}
# 트리거 +0.5% — 구버전이라면 즉시 전량 집행되던 지점
s8._try_fill_entry_plan("TB", 10_050, now=now8 + 5)
check("[실코드] 상승 이탈 가격에도 매수 0건", "TB" not in s8.holdings,
      f"주문 {len(s8.order_manager.orders)}건")
check("[실코드] 계획은 살아서 되돌림을 계속 기다린다", "TB" in s8._entry_plans)

# 대조군 — 되돌림은 정상 체결돼야 한다(막히면 매매가 통째로 죽는다)
s8._try_fill_entry_plan("TB", 9_960, now=now8 + 10)
check("[실코드·대조군] 되돌림 -0.4%는 정상 체결", "TB" in s8.holdings,
      f"보유 {s8.holdings.get('TB', {}).get('qty', 0)}주")

# ═════════════════════════════════════════════════════════
print("\n[5] [H] 파동 상한 — 같은 종목 반복 매수 (그날 6종목이 재매수됨)")
# ═════════════════════════════════════════════════════════
repeat = {}
for t, code, name, *_ in TRADES:
    repeat.setdefault(code, []).append(t)
multi = {c: v for c, v in repeat.items() if len(v) > 1}
check("그날 재매수된 종목이 6개", len(multi) == 6,
      ", ".join(f"{c}x{len(v)}" for c, v in multi.items()))

s9 = build(datetime(2026, 8, 6, 9, 30, 0))
setup(s9, "WV", open_px=10_000)
now9 = time.time()
waves = []
for i in range(6):
    at = now9 + i * 120          # 쿨다운(60초)보다 크게 -> 매번 새 파동
    s9._strength_since["WV"] = at - 10
    feed(s9.phase1b.trade_flow, "WV", 2,
         SM.PHASE1A_BURST_TRADE_VALUE * 1.3, now=at, price=10_000)
    ok, info = s9.evaluate_tick_entry("WV", "1A", 10_000,
                                      open_price=10_000, now=at)
    waves.append((info.get("burst_wave"), ok))
check("[실코드] 1~3번째 파동은 통과", [w[1] for w in waves[:3]] == [True] * 3,
      f"{[w[0] for w in waves[:3]]}")
check("[실코드] 4번째부터 차단", [w[1] for w in waves[3:]] == [False] * 3,
      f"{[w[0] for w in waves[3:]]}")
check("[실코드] 상한 초과 후에도 카운트는 계속 돈다(순번 유지)",
      [w[0] for w in waves] == [1, 2, 3, 4, 5, 6], f"{[w[0] for w in waves]}")

s10 = build(datetime(2026, 8, 6, 9, 30, 0))
setup(s10, "CD", open_px=10_000)
now10 = time.time()
for i in range(5):               # 쿨다운 안의 연속 발화 = 같은 파동 하나
    s10._note_burst_wave("CD", now=now10 + i * 5)
check("[실코드] 쿨다운(60초) 안 연속 발화는 1파동으로 접힘",
      len(s10._burst_waves["CD"]) == 1, f"{len(s10._burst_waves['CD'])}")

# ═════════════════════════════════════════════════════════
print("\n[6] [B] 버스트 매수방향 — 투매를 매수신호로 읽지 않는가")
# ═════════════════════════════════════════════════════════
check("BURST_REQUIRE_BUY_SIDE == True", SM.BURST_REQUIRE_BUY_SIDE is True)

s11 = build(datetime(2026, 8, 6, 9, 30, 0))
setup(s11, "SELLB", open_px=10_000)
now11 = time.time()
s11._strength_since["SELLB"] = now11 - 30      # FID228 당일누적이라 급락 중에도 100+
feed(s11.phase1b.trade_flow, "SELLB", 4,
     SM.PHASE1A_BURST_TRADE_VALUE * 2.0, now=now11, price=10_000, side="sell")
okb, infob = s11.check_burst("SELLB", now=now11)
check("[실코드] 대량 '매도' 체결만으로는 버스트 미발화", not okb,
      str(infob.get("reason", ""))[:52])

s12 = build(datetime(2026, 8, 6, 9, 30, 0))
setup(s12, "BUYB", open_px=10_000)
now12 = time.time()
s12._strength_since["BUYB"] = now12 - 30
feed(s12.phase1b.trade_flow, "BUYB", 4,
     SM.PHASE1A_BURST_TRADE_VALUE * 2.0, now=now12, price=10_000, side="buy")
okb2, _ = s12.check_burst("BUYB", now=now12)
check("[실코드·대조군] 같은 규모가 '매수'면 정상 발화", okb2)

# ═════════════════════════════════════════════════════════
print("\n[7] [F] 진입 숙성 — 개장 직후 급한 진입 차단")
# ═════════════════════════════════════════════════════════
check("MIN_ENTRY_DELAY_SEC == 60", SM.MIN_ENTRY_DELAY_SEC == 60.0)

s13 = build(datetime(2026, 8, 6, 9, 0, 31))
setup(s13, "006340", open_px=15_260, ripe=False)
nowf = time.time()
s13._first_seen["006340"] = nowf - 31          # 09:00 편입 -> 09:00:31 매수
r13 = s13._entry_delay_reject("006340", now=nowf)
check("[실코드] 편입 31초 후 매수 시도 -> 차단", bool(r13), str(r13)[:52])

s14 = build(datetime(2026, 8, 6, 9, 10, 13))
setup(s14, "004310", open_px=5_925, ripe=False)
nowg = time.time()
s14._first_seen["004310"] = nowg - 613         # 09:00 편입 -> 09:10:13 매수
check("[실코드·승자] 현대약품(편입 613초 후)은 통과",
      s14._entry_delay_reject("004310", now=nowg) is None)

# 참고치 — 편입 시각이 DB에 없어 '09:00 편입'을 가정한 계산이다(단언 아님).
def _sec(hms):
    h, m, sec = (int(x) for x in hms.split(":"))
    return (h - 9) * 3600 + m * 60 + sec
early = [t for t in TRADES if _sec(t[0]) < 60]
print(f"       (참고) 09:00 편입 가정 시 숙성에 걸리는 건: {len(early)}건 — "
      + ", ".join(f"{t[2]} {t[0]}({t[6]:+.2f}%)" for t in early))

# ═════════════════════════════════════════════════════════
print("\n[8] 종합 — 손실 7건 vs 승자 4건의 운명")
# ═════════════════════════════════════════════════════════
def verdict(t, code, name, px, sus, path, pnl, chg, opn):
    """현행 코드에서 이 건이 막히는 이유(있으면)."""
    if sus < SM.TICK_STRENGTH_SUSTAIN_SEC:
        return f"무장 {sus}초 < {SM.TICK_STRENGTH_SUSTAIN_SEC}초"
    if chg <= SM.MIN_ENTRY_CHANGE_PCT:
        return f"등락률 하한 {chg:+.2f}%"
    if opn >= SM.PHASE1A_LEADING_OPEN_SURGE_CAP:
        return f"시가대비 +{opn:.2f}%"
    return None

blocked, survived = [], []
for row in TRADES:
    why = verdict(*row)
    (blocked if why else survived).append((row, why))

print(f"       차단 {len(blocked)}건 / 통과 {len(survived)}건")
for (t, c, n, px, sus, pa, pnl, ch, op), why in blocked:
    print(f"         차단 {t} {n:<12} {pnl:+6.2f}%  <- {why}")

lost_blocked = sum(p for (t, c, n, px, s_, pa, p, ch, op), w in blocked if p <= -3.0)
n_lost_blocked = sum(1 for (t, c, n, px, s_, pa, p, ch, op), w in blocked if p <= -3.0)
# ⚠️ **여기서 셀 수 있는 것은 결정적 게이트 3개뿐이다**(무장 / 등락률 하한 /
#    시가대비). [D] 상승이탈 OFF·[F] 숙성·[H] 파동은 그날의 편입 시각과 틱
#    흐름이 있어야 판정되는데 둘 다 없다(틱 아카이브는 09:20 태스크가 09:19:01
#    정지로 20초 차이로 못 돌아 존재하지 않고, 편입 시각은 DB에 없다).
#    실제 차단은 이보다 **많다** — 문서 기준 그날 매수 19건 중 15건이
#    상승이탈 경로였고 [D]로 그 경로 자체가 꺼졌다. 여기서는 **과장하지 않고**
#    결정적으로 증명되는 2건만 못박는다.
check("결정적 게이트만으로 손절 2건이 차단된다(무장 3초)",
      n_lost_blocked == 2,
      f"{n_lost_blocked}건 / 합계 {lost_blocked:+.2f}% "
      f"([D][F][H]는 틱·편입시각 부재로 정량화 불가 — 실제 차단은 이보다 많다)")
check("🔴 승자 4건은 **한 건도** 차단되지 않는다",
      not any((t, c) in WINNERS for (t, c, *_), w in blocked),
      ", ".join(n for (t, c, n, *_), w in blocked if (t, c) in WINNERS) or "차단 0건")

gross_all = sum(t[6] for t in TRADES)
gross_after = sum(r[6] for r, w in survived)
print(f"       가격기준 합계: 그날 {gross_all:+.2f}% -> 현행 코드 {gross_after:+.2f}%")
check("차단 후 합계가 개선된다", gross_after > gross_all,
      f"{gross_all:+.2f}% -> {gross_after:+.2f}%")

# ═════════════════════════════════════════════════════════
print("\n[9] 게이트 상호 충돌 — 한 규칙이 다른 규칙을 무력화하지 않는가")
# ═════════════════════════════════════════════════════════
# 되돌림 대기 중 [C] 하한을 뚫고 체결되지 않는가 (가격이 내려가면 등락률도 내려간다)
# ⚠️ 전일종가를 **직접 심는다**. 스텁의 get_stock_change_rate는 넘겨준 가격과
#    무관하게 고정 등락률을 돌려주므로, 캐시가 비어 있으면 _get_prev_close가
#    '내려간 가격'으로 전일종가를 다시 역산해 등락률이 영원히 +0.20%로 나온다
#    (= 하한 판정이 통째로 무의미해진다). 실물은 pre-arm이 편입 시점 가격으로
#    한 번만 캐시하므로 이렇게 심는 쪽이 실제 동작이다.
s15 = build(datetime(2026, 8, 6, 9, 30, 0), change_rate=0.20)
setup(s15, "LB", open_px=10_000)
s15._prev_closes["LB"] = 9_980.0          # 트리거 10,000 = 전일대비 +0.20%
now15 = time.time()
s15._entry_plans["LB"] = {
    "trigger_price": 10_000,
    "targets": [{"price": 9_970, "frac": 0.5, "depth": 0.003, "filled": False},
                {"price": 9_930, "frac": 0.5, "depth": 0.007, "filled": False}],
    "deadline": now15 + 120, "sub_strategy": "1A",
    "info": {"current_price": 10_000}, "cond_name": "주도주상위",
    "phase": 1, "stock_name": "LB",
}
# 전일종가 9,980(=+0.20%) 기준으로 9,930은 전일 대비 -0.50% -> 하한 위반
s15._try_fill_entry_plan("LB", 9_930, now=now15 + 5)
check("되돌림이 깊어 전일종가를 밑돌면 _execute_buy가 막는다",
      "LB" not in s15.holdings,
      f"보유 {'있음' if 'LB' in s15.holdings else '없음'}")

# 손절은 어떤 게이트보다도 우선인가 (숙성/파동/VWAP이 매도를 막으면 안 된다)
s16 = build(datetime(2026, 8, 6, 9, 30, 0))
setup(s16, "SL", open_px=10_000, ripe=False)   # 숙성 미달 상태
s16._first_seen["SL"] = time.time()            # 방금 편입
s16.holdings["SL"] = {
    "trade_id": 1, "buy_price": 10_000, "origin_price": 10_000,
    "buy_quantity": 100, "qty": 100, "buy_time": s16._now(),
    "stock_name": "SL", "strategy_phase": "1A", "sub_strategy": "1A",
    "highest_price": 10_000, "lowest_price": 10_000, "ma20": None,
    "ma20_updated": None,
    "warmup_until": s16._now() + _td(seconds=999),   # 워밍업 중까지 겹쳐서
}
s16.on_price_update("SL", 9_600)               # -4.0%
check("숙성 미달 + 워밍업 중이라도 손절은 정상 발동", "SL" not in s16.holdings,
      f"잔존 {list(s16.holdings)}")

# VWAP 필터가 '모름'을 차단으로 바꾸지 않는가 (2026-08-07 ON 이후에도 불변)
# 🔴 이게 핵심이다 — VWAP 미수신(0)이면 통과시켜야 한다. 안 그러면 세션 초반
#    이나 데이터가 안 오는 종목에서 매수가 통째로 죽는데, 로그를 뒤지기 전엔
#    안 보인다(08-05에 시가필터가 정확히 그 이유로 하루 종일 0건이었다).
s17 = build(datetime(2026, 8, 6, 9, 30, 0))
setup(s17, "VW", open_px=10_000)
check("VWAP_ENTRY_ENABLED == True (08-07 장마감 후 ON)",
      SM.VWAP_ENTRY_ENABLED is True)
check("🔴 ON이어도 VWAP 미수신이면 통과(모름이 매수를 막지 않는다)",
      s17.vwap_entry_reject("VW", 10_000) is None)
# 09:05 이전도 판정하지 않는다(VWAP이 요동치는 구간)
check("ON이어도 09:05 이전에는 판정하지 않는다",
      s17.vwap_entry_reject("VW", 10_000,
                            now_dt=datetime(2026, 8, 6, 9, 4, 59)) is None)

# ═════════════════════════════════════════════════════════
print("\n[10] 조건검색 거래소 — KRX 전환이 계층 정합을 이루는가")
# ═════════════════════════════════════════════════════════
from api import kiwoom_ws as KWS
import inspect as _insp

check("CONDITION_STEX_TP == 'K' (KRX)", KWS.CONDITION_STEX_TP == "K",
      repr(KWS.CONDITION_STEX_TP))
src = _insp.getsource(KWS)
check("조건검색 두 경로가 상수를 공유(하드코딩 없음)",
      src.count('stex_tp="K"') == 0 and src.count("stex_tp='K'") == 0,
      "리터럴 하드코딩 0건")
check("주문/잔고는 KRX 고정 유지(조건검색과 독립)",
      src.count('CONDITION_STEX_TP') >= 2, "공통 상수 참조")
check("core/ 전략 로직은 거래소 상수를 모른다(진입 판정 불변)",
      "CONDITION_STEX_TP" not in _insp.getsource(SM), "core 미참조")

# ═════════════════════════════════════════════════════════
print("\n[11] [C] x 손절대신추가매수 — 막힐 때 손절로 수렴하는가 (신규 교차)")
# ═════════════════════════════════════════════════════════
# 🔴 아무 테스트도 덮지 않던 상호작용이다.
#    rescue-add는 진입가 -3%에서 발동하는데, [C]는 전일종가 대비 0% 초과를
#    요구한다. 진입 시점 전일대비가 **+3.09% 미만**이었으면 -3% 지점의 가격은
#    전일종가를 밑돌아 [C]가 `_execute_buy`를 막는다(진입 상한이 13%이므로
#    0~3.09% 구간 = 실전에서 흔한 구간이 전부 해당).
#    이때 `_execute_buy`는 **예외 없이 조용히 return** 한다 —
#    `_do_rescue_add`가 이걸 '미체결'로 감지하지 못하면 포지션이 관찰 상태로
#    남아 -6% 최종손절까지 흘러간다. 그 수렴을 여기서 못박는다.
def _rescue_pos(s, code, buy=10_000):
    s.holdings[code] = {
        "trade_id": 1, "buy_price": buy, "origin_price": buy,
        "buy_quantity": 100, "qty": 100, "buy_time": s._now(),
        "stock_name": code, "strategy_phase": "1A", "sub_strategy": "1A",
        "highest_price": buy, "lowest_price": buy, "ma20": None,
        "ma20_updated": None,
        "warmup_until": s._now() + _td(seconds=-1),
    }
    return s.holdings[code]


def _rescue_feed(s, code, base=100.0):
    """추가매수 조건 3개(가속·강도·반등)를 전부 성립시킨다."""
    s.phase1b.start_watching(code)
    tf = s.phase1b.trade_flow
    n = time.time()
    for i in range(40):
        tf.add_tick(code, 10_000, "buy", 1, now=n - 110 + i)
    for i in range(10):
        tf.add_tick(code, 9_700, "sell", 1, now=n - 28 + i)
    for i in range(10):
        tf.add_tick(code, 9_730, "buy", 400, now=n - 10 + i)
    s.holdings[code]["strength_baseline"] = base


check("RESCUE_ADD_ENABLED == True (이 교차가 실제로 도달 가능)",
      SM.RESCUE_ADD_ENABLED is True)

# ── 차단 케이스: 전일종가 9,900 -> -3% 지점(9,690)은 전일대비 -2.12% ──
sr = build(datetime(2026, 8, 6, 10, 0, 0), change_rate=-2.12)
setup(sr, "RC")
sr._prev_closes["RC"] = 9_900.0
_rescue_pos(sr, "RC")
_rescue_feed(sr, "RC")
sr.on_price_update("RC", 9_690)      # 첫 -3.1% -> 관찰 시작
sr.on_price_update("RC", 9_650)      # 저점 갱신
sr.on_price_update("RC", 9_690)      # 저점 대비 +0.41% 반등 -> 집행 시도
buys_r = [o for o in sr.order_manager.orders if o["side"] == "buy"]
sells_r = [o for o in sr.order_manager.orders if o["side"] == "sell"]
check("[C]가 막으면 추가매수는 0건", len(buys_r) == 0, f"매수 {len(buys_r)}건")
check("🔴 그때 **손절로 수렴한다**(관찰 상태로 방치되지 않는다)",
      len(sells_r) >= 1 and "RC" not in sr.holdings,
      f"매도 {len(sells_r)}건 / 잔존 {list(sr.holdings)}")

# ── 대조군: 전일종가 9,400 -> -3% 지점은 전일대비 +3.09% (하한 통과) ──
sr2 = build(datetime(2026, 8, 6, 10, 0, 0), change_rate=3.09)
setup(sr2, "RD")
sr2._prev_closes["RD"] = 9_400.0
_rescue_pos(sr2, "RD")
_rescue_feed(sr2, "RD")
sr2.on_price_update("RD", 9_690)
sr2.on_price_update("RD", 9_650)
sr2.on_price_update("RD", 9_690)
buys_r2 = [o for o in sr2.order_manager.orders if o["side"] == "buy"]
check("[대조군] 하한을 넘으면 추가매수가 정상 집행된다(기능이 죽지 않았다)",
      len(buys_r2) == 1, f"매수 {len(buys_r2)}건")
check("[대조군] 원가는 평단으로 덮이지 않는다(최종손절 기준 보존)",
      sr2.holdings.get("RD", {}).get("origin_price") == 10_000,
      f"원가 {sr2.holdings.get('RD', {}).get('origin_price')}")

print("       (해석) 진입 시점 전일대비 +3.09% 미만이면 rescue-add는 [C]에 막혀")
print("              손절로 수렴한다. 안전하지만 **기능 발동률이 문서보다 낮다** —")
print("              추가 리스크는 없고(최대 -4.5%는 그대로), 관찰 15초만 소모된다.")

print("\n" + "=" * 66)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
if FAIL:
    print("실패 항목:")
    for f in FAIL:
        print("  -", f)
print("=" * 66)
sys.exit(1 if FAIL else 0)
