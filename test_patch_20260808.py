"""2026-08-08 패치 격리 검증 — 발사 게이트 시간대 분리.

08-08 틱 아카이브 분석(5일 146 종목·일, 09:02~09:10, +5분, **독립표본**)에서
현행 버스트가 기준선보다 나쁘다는 것이 확인됐다:

    [기준선] 그냥 매수   n=146  +0.395%  플러스일 4/5
    현행 버스트           n= 28  -0.349%  플러스일 **1/5**
    09:02~09:05 버스트만  n= 12  -0.895%  플러스일 1/3
    09:05~ 가속 2.5       n= 54  +0.688%  플러스일 **4/5**

그래서 발사 조건을 시간대로 나눴다(사용자 지정):
  · 09:00~09:05 : 발사 게이트 **면제**(무장만으로 발사)
  · 09:05~      : 버스트 **대신** 거래대금 가속도 >= 2.5

이 파일이 못박는 것은 네 가지다:
  ① 시각에 따라 정확히 갈리는가(경계 포함)
  ② **버스트를 더한 게 아니라 대체했는가** — 09:05 이후엔 버스트 규모
     대량체결이 있어도 가속이 미달이면 사지 않아야 한다
  ③ 바뀐 발사 경로에서도 **파동 상한[H]과 나머지 게이트가 그대로 사는가**
  ④ check_burst 자체는 재매수·슬롯교체용으로 **그대로 남아있는가**

네트워크·DB·키움 API를 타지 않는 순수 격리 테스트.
실행: python test_patch_20260808.py   (종료코드 0 = 전원 통과)
"""
import sys
import time
from datetime import datetime, timedelta, time as dtime

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
# 스텁 (test_patch_20260804/05/06과 동일 계약)
# ⚠️ 다른 테스트 파일을 import하지 말 것 — 그 파일이 통째로 실행되고
#    마지막 sys.exit()에서 이 파일이 끊긴다(08-05 심야에 실제로 겪었다).
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
        return [{"time_str": "20260810090000", "open": 9_990, "high": 10_010,
                 "low": 9_980, "close": 10_000, "volume": 1_000}] * max(1, count)

    def get_orderable_amount(self): return 10_000_000
    def get_stock_change_rate(self, code): return self.change_rate
    def get_basic_quote(self, code): return {"change_rate": self.change_rate}
    def get_index_change_rate(self, s="001"): return 0.0
    def get_current_price(self, code): return 10_000
    def get_daily_candles(self, code, count=30, base_date=None): return []


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


def build(now_dt=datetime(2026, 8, 10, 9, 6, 0), change_rate=3.0):
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


def setup(strat, code, cond="주도주상위", ask=10_000, open_px=10_000):
    strat._first_seen[code] = time.time() - 999   # [F] 숙성 완료 상태
    strat._cond_names[code] = cond
    strat._stock_names[code] = code
    strat.watch_list_today.add(code)
    if open_px:
        strat._opening_prices[code] = open_px
    strat.phase1b.start_watching(code)
    strat.phase1b.orderbook.update(
        code, {"ask_prices": [ask, ask + 10, ask + 20],
               "ask_volumes": [3_000, 3_000, 3_000]}, now=time.time())


def feed_uniform(tf, code, total_value, t_end, span, price=10_000, side="buy", n=40):
    """[t_end-span, t_end) 구간에 total_value를 **균일하게** 흘린다."""
    per = max(1, int(total_value / n // price))
    for i in range(n):
        tf.add_tick(code, price, side, per, now=t_end - span * (i + 0.5) / n)


def arm(strat, code, t0, price=10_000):
    """무장만 시킨다(발사 조건은 각 섹션이 직접 만든다)."""
    strat.on_trade({"stock_code": code, "price": price, "side": "buy",
                    "volume": 10, "strength": 130.0}, now=t0)
    strat.on_trade({"stock_code": code, "price": price, "side": "buy",
                    "volume": 10, "strength": 130.0},
                   now=t0 + SM.TICK_STRENGTH_SUSTAIN_SEC + 0.5)


T = time.time()

# ═════════════════════════════════════════════════════════
print("\n[1] 상수·불변식")
# ═════════════════════════════════════════════════════════
check("발사 분리 ON", SM.FIRE_GATE_SPLIT_ENABLED is True)
check("전환 시각 09:05", SM.FIRE_GATE_ACCEL_FROM == dtime(9, 5),
      str(SM.FIRE_GATE_ACCEL_FROM))
check("요구 가속 2.5", SM.FIRE_ACCEL_MIN == 2.5, str(SM.FIRE_ACCEL_MIN))
check("창 30초/120초", (SM.FIRE_ACCEL_SHORT_SEC, SM.FIRE_ACCEL_LONG_SEC) == (30.0, 120.0))

# 🔴 LONG이 버퍼보다 길면 **경고 없이 잘린 값**이 나온다.
check("LONG == TradeFlowTracker 기본 버퍼창 (조용한 절단 방지)",
      SM.FIRE_ACCEL_LONG_SEC == TradeFlowTracker().max_window_sec,
      f"{SM.FIRE_ACCEL_LONG_SEC} vs {TradeFlowTracker().max_window_sec}")
check("SHORT < LONG (가속도 정의 성립)",
      SM.FIRE_ACCEL_SHORT_SEC < SM.FIRE_ACCEL_LONG_SEC)
# 가속도의 수학적 상한 = LONG/SHORT. 문턱이 이걸 넘으면 영원히 발화하지 않는다.
_cap = SM.FIRE_ACCEL_LONG_SEC / SM.FIRE_ACCEL_SHORT_SEC
check("요구 가속 < 수학적 상한(LONG/SHORT) — 도달 불가능한 문턱 방지",
      SM.FIRE_ACCEL_MIN < _cap, f"{SM.FIRE_ACCEL_MIN} < {_cap}")
# ⚠️ 절대 대금 하한을 두지 않는 것이 **의도**다(실측이 정반대 방향).
check("절대 대금 하한 상수를 만들지 않았다(실측이 반대 방향)",
      not any(hasattr(SM, n) for n in
              ("FIRE_ACCEL_MIN_VALUE", "FIRE_ACCEL_VALUE_FLOOR")))
check("전환 시각이 VWAP 시작과 같다(게이트 경계 일치)",
      SM.FIRE_GATE_ACCEL_FROM == SM.VWAP_ENTRY_FROM,
      f"{SM.FIRE_GATE_ACCEL_FROM} / {SM.VWAP_ENTRY_FROM}")

# ═════════════════════════════════════════════════════════
print("\n[2] 09:00~09:05 — 발사 게이트 면제")
# ═════════════════════════════════════════════════════════
s = build(datetime(2026, 8, 10, 9, 2, 0))
setup(s, "AAA")
ok, det = s._fire_gate("AAA", now=T, now_dt=datetime(2026, 8, 10, 9, 2, 0))
check("09:02 — 틱이 하나도 없어도 발사 통과", ok is True, str(det.get("trigger")))
check("09:02 — 경로 표기가 '개장초반'", det.get("burst_path") == "개장초반")
check("09:02 — fire_gate 태그", det.get("fire_gate") == "early_open")

ok0, _ = s._fire_gate("AAA", now=T, now_dt=datetime(2026, 8, 10, 9, 0, 0))
check("[경계] 09:00:00 면제", ok0 is True)
okb, _ = s._fire_gate("AAA", now=T, now_dt=datetime(2026, 8, 10, 9, 4, 59))
check("[경계] 09:04:59 면제", okb is True)
oka, deta = s._fire_gate("AAA", now=T, now_dt=datetime(2026, 8, 10, 9, 5, 0))
check("[경계] 09:05:00부터 가속 판정으로 전환",
      oka is False and deta.get("fire_gate") == "accel", str(deta.get("reason"))[:60])

# ═════════════════════════════════════════════════════════
print("\n[3] 09:05~ — 거래대금 가속도 판정")
# ═════════════════════════════════════════════════════════
s3 = build(datetime(2026, 8, 10, 9, 6, 0))
setup(s3, "BBB")
tf3 = s3.phase1b.trade_flow
NOW = T
# 120초 구간에 1억을 균일하게 -> 가속 1.0 근처
feed_uniform(tf3, "BBB", 100_000_000, NOW, span=120.0)
a_flat = tf3.value_acceleration("BBB", short_sec=30, long_sec=120, now=NOW)
ok3, det3 = s3._fire_gate("BBB", now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0))
check("균일 유입(가속~1.0)이면 탈락", ok3 is False and a_flat < 1.5, f"accel={a_flat:.2f}")
check("탈락 사유에 '거래대금 가속'이 들어간다(진단 분류용)",
      "거래대금 가속" in det3.get("reason", ""), det3.get("reason", "")[:70])
check("detail에 실제 가속값이 실린다(사후 추적용)",
      abs(det3.get("accel", 0) - a_flat) < 0.01, str(det3.get("accel")))

# 최근 30초에 몰아넣어 가속을 올린다
s3b = build(datetime(2026, 8, 10, 9, 6, 0))
setup(s3b, "BBB")
tfb = s3b.phase1b.trade_flow
feed_uniform(tfb, "BBB", 40_000_000, NOW - 30.0, span=90.0)   # 앞 90초
feed_uniform(tfb, "BBB", 90_000_000, NOW, span=29.0)          # 최근 30초에 집중
a_hot = tfb.value_acceleration("BBB", short_sec=30, long_sec=120, now=NOW)
okh, deth = s3b._fire_gate("BBB", now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0))
check("최근 30초 집중(가속 >= 2.5)이면 통과",
      okh is True and a_hot >= SM.FIRE_ACCEL_MIN, f"accel={a_hot:.2f}")
check("통과 시 trigger에 가속 배수가 남는다",
      "거래대금 가속" in deth.get("trigger", ""), deth.get("trigger", "")[:70])
check("통과 시 경로 표기가 '가속'", deth.get("burst_path") == "가속")

# '모름'을 통과시키지 않는다 — 발사는 매수를 만드는 조건이라 방향이 반대다.
s3c = build(datetime(2026, 8, 10, 9, 6, 0))
setup(s3c, "CCC")
okc, detc = s3c._fire_gate("CCC", now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0))
check("틱이 전혀 없으면 **탈락**(모름을 통과시키지 않는다)", okc is False)

# ── 🔴 최소 틱수 가드 — 데이터가 없을수록 쉽게 뚫리던 구조를 닫는다 ──
# (기존 test_patch_20260802의 무장 TTL 2건이 이 결함을 잡아냈다)
s3e = build(datetime(2026, 8, 10, 9, 6, 0))
setup(s3e, "SPARSE")
tfe = s3e.phase1b.trade_flow
# 틱 2개, 둘 다 최근 30초 안 -> 가속도는 **수학적 최대 4.0**이 된다
tfe.add_tick("SPARSE", 10_000, "buy", 10, now=NOW - 10.0)
tfe.add_tick("SPARSE", 10_000, "buy", 10, now=NOW - 5.0)
a_sparse = tfe.value_acceleration("SPARSE", short_sec=30, long_sec=120, now=NOW)
check("[재현] 틱 2개면 가속도가 상한(4.0)까지 튄다",
      a_sparse >= SM.FIRE_ACCEL_LONG_SEC / SM.FIRE_ACCEL_SHORT_SEC - 0.01,
      f"accel={a_sparse:.2f}")
oks, dets = s3e._fire_gate("SPARSE", now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0))
check("[가드] 그런데 틱수 미달이라 발사되지 않는다", oks is False,
      dets.get("reason", "")[:70])
check("[가드] 사유가 '체결틱 부족'이라 인프라 분류를 그대로 탄다",
      SM.StrategyManager._reject_category(dets["reason"]) == "체결틱 부족(강도판단불가)",
      SM.StrategyManager._reject_category(dets["reason"]))
check("[가드] detail에 틱수가 실린다", dets.get("accel_ticks") == 2,
      str(dets.get("accel_ticks")))

# 경계 — 정확히 MIN_TICKS면 통과해야 한다(가속만 충족하면)
s3f = build(datetime(2026, 8, 10, 9, 6, 0))
setup(s3f, "EDGE")
tff = s3f.phase1b.trade_flow
for i in range(SM.FIRE_ACCEL_MIN_TICKS):
    tff.add_tick("EDGE", 10_000, "buy", 100, now=NOW - 25.0 + i * 0.5)
n_edge = tff.tick_count("EDGE", SM.FIRE_ACCEL_LONG_SEC, now=NOW)
oke, dete = s3f._fire_gate("EDGE", now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0))
check(f"[경계] 정확히 {SM.FIRE_ACCEL_MIN_TICKS}틱이면 가드를 통과한다",
      n_edge == SM.FIRE_ACCEL_MIN_TICKS and oke is True,
      f"틱{n_edge} ok={oke}")
check("[가드] 금액 하한이 아니라 **개수** 하한이다(값 하한 상수 없음)",
      isinstance(SM.FIRE_ACCEL_MIN_TICKS, int) and SM.FIRE_ACCEL_MIN_TICKS == 20,
      str(SM.FIRE_ACCEL_MIN_TICKS))

# 인프라 사유는 기존 문자열을 그대로 써야 진단에서 인프라 경고로 잡힌다.
s3d = build(datetime(2026, 8, 10, 9, 6, 0))
s3d.phase1b = None
okd, detd = s3d._fire_gate("DDD", now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0))
check("데이터소스 없으면 기존 인프라 사유를 재사용",
      okd is False and "데이터 소스 없음" in detd.get("reason", ""),
      detd.get("reason", ""))
check("그 사유가 인프라 경고로 분류된다",
      SM.StrategyManager._reject_category(detd["reason"]) in SM.StrategyManager._REJECT_INFRA,
      SM.StrategyManager._reject_category(detd["reason"]))

# ═════════════════════════════════════════════════════════
print("\n[4] 🔴 '더한 게 아니라 대체' — 실제 진입 경로에서 확인")
# ═════════════════════════════════════════════════════════
# 09:06에 **버스트 규모** 대량체결(4천만+ 2건)을 넣되 균일 유입이라 가속은 낮다.
s4 = build(datetime(2026, 8, 10, 9, 6, 0))
setup(s4, "EEE")
tf4 = s4.phase1b.trade_flow
scale = SM.burst_price_scale(10_000)
big = SM.PHASE1A_BURST_TRADE_VALUE * scale * 1.05
# 앞 90초에 큰 체결을 흩어 놓아 절대·단일 경로는 성립하되 가속은 눌러 둔다
for i in range(2):
    tf4.add_tick("EEE", 10_000, "buy", int(big // 10_000), now=NOW - 1.0 - i * 0.3)
feed_uniform(tf4, "EEE", 600_000_000, NOW - 5.0, span=110.0)
burst_ok, _bd = s4.check_burst("EEE", now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0))
a4 = tf4.value_acceleration("EEE", short_sec=30, long_sec=120, now=NOW)
fire_ok, fd = s4._fire_gate("EEE", now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0))
check("[대체 증명] 구 버스트는 성립하는 상황", burst_ok is True)
check("[대체 증명] 그런데 가속 미달이라 발사되지 않는다",
      fire_ok is False, f"accel={a4:.2f} < {SM.FIRE_ACCEL_MIN}")

# 같은 상황을 evaluate_tick_entry(실제 진입 평가)로 한 번 더
arm(s4, "EEE", NOW - 10.0)
ev_ok, ev = s4.evaluate_tick_entry(
    "EEE", "1A", 10_000, open_price=10_000, cond_name="주도주상위",
    now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0),
)
check("[대체 증명] evaluate_tick_entry도 같은 이유로 탈락",
      ev_ok is False and "거래대금 가속" in ev.get("reason", ""),
      ev.get("reason", "")[:70])
check("[진단] 그 사유가 '거래대금 가속 미달'로 분류된다",
      SM.StrategyManager._reject_category(ev["reason"]) == "거래대금 가속 미달",
      SM.StrategyManager._reject_category(ev["reason"]))
check("[진단] '대량체결 부족'과 뭉개지지 않는다",
      SM.StrategyManager._reject_category(ev["reason"]) != "대량체결 부족")
check("[진단] 인프라 경고가 아니다(정상 필터링)",
      "거래대금 가속 미달" not in SM.StrategyManager._REJECT_INFRA)
check("[진단] 놓친기회 계층에 등록돼 있다(09:05 이후 🥈가 비지 않게)",
      any(lbl == "거래대금 가속 미달" for _m, lbl, _d in SM.StrategyManager._MISS_TIERS))

# 09:02(면제 구간)에서는 버스트 없이도 통과해야 한다
s4b = build(datetime(2026, 8, 10, 9, 2, 0))
setup(s4b, "FFF")
arm(s4b, "FFF", NOW - 10.0)
ev2_ok, ev2 = s4b.evaluate_tick_entry(
    "FFF", "1A", 10_000, open_price=10_000, cond_name="주도주상위",
    now=NOW, now_dt=datetime(2026, 8, 10, 9, 2, 0),
)
check("09:02 — 버스트 없이 무장만으로 진입 평가 통과", ev2_ok is True,
      ev2.get("reason", "")[:70])
check("09:02 — entry_reason에 면제 표기가 남는다",
      "개장초반" in ev2.get("reason", ""), ev2.get("reason", "")[:70])

# ═════════════════════════════════════════════════════════
print("\n[5] 바뀐 발사 경로에서도 나머지 게이트가 그대로 사는가")
# ═════════════════════════════════════════════════════════
# [H] 파동 상한 — 면제 구간에서도 **진짜 폭발**은 4번째에서 막혀야 한다.
#
# 🔴 (2026-08-09 사양 정정) 이 블록은 원래 거래대금이 **0원인** 종목으로
# 1,2,3번째 통과 -> 4번째 차단을 확인했다. 그건 결함을 사양으로 굳힌
# 것이었다 — 면제 구간의 _fire_gate가 무조건 True라 폭발이 하나도 없어도
# 60초마다 파동이 쌓였고, 실측상 09:15까지 158종목 중 106개(67%)가 상한을
# 넘겨 **하루 종일 매수 불가**가 됐다. 파동은 '폭발 횟수'를 세는 지표이지
# '게이트 통과 횟수'가 아니다(BURST_WAVE_COUNT_REQUIRES_BURST 주석 참고).
# -> 브레이크가 살아있음은 **진짜 폭발을 반복하는 종목**으로 확인한다.


def _boom(strat, code, t):
    """실제 대량체결(4천만원 문턱 초과 x 2건)을 만들어 check_burst를 성립시킨다."""
    for k in range(3):
        strat.phase1b.trade_flow.add_tick(code, 10_000, "buy", 6_000,
                                          now=t - 2 + k * 0.5)


s5 = build(datetime(2026, 8, 10, 9, 2, 0))
setup(s5, "GGG")
arm(s5, "GGG", NOW - 10.0)
waves = []
for i in range(SM.BURST_WAVE_MAX + 1):
    t = NOW + i * (SM.BURST_WAVE_COOLDOWN_SEC + 1)
    _boom(s5, "GGG", t)
    o, d = s5.evaluate_tick_entry(
        "GGG", "1A", 10_000, open_price=10_000, cond_name="주도주상위",
        now=t, now_dt=datetime(2026, 8, 10, 9, 2, 0),
    )
    waves.append((o, d.get("burst_wave"), d.get("reason", "")))
check(f"[H] 면제 구간에서도 1~{SM.BURST_WAVE_MAX}번째 파동은 통과",
      all(w[0] for w in waves[:SM.BURST_WAVE_MAX]),
      str([w[1] for w in waves]))
check(f"[H] {SM.BURST_WAVE_MAX + 1}번째 파동은 차단",
      waves[-1][0] is False and "버스트 파동" in waves[-1][2], waves[-1][2][:60])

# 🔴 결함 회귀방지 — 거래대금이 0원이면 파동은 **한 개도** 쌓이지 않는다.
# (수정 전엔 여기서 1,2,3,4로 쌓여 4번째부터 하루 종일 매수 불가였다)
s5q = build(datetime(2026, 8, 10, 9, 2, 0))
setup(s5q, "QUIET")
arm(s5q, "QUIET", NOW - 10.0)
quiet = []
for i in range(SM.BURST_WAVE_MAX + 2):
    t = NOW + i * (SM.BURST_WAVE_COOLDOWN_SEC + 1)
    o, d = s5q.evaluate_tick_entry(
        "QUIET", "1A", 10_000, open_price=10_000, cond_name="주도주상위",
        now=t, now_dt=datetime(2026, 8, 10, 9, 2, 0),
    )
    quiet.append((o, d.get("burst_wave")))
check("🔴 [회귀] 폭발이 없으면 면제 구간에서 파동이 쌓이지 않는다",
      all(w[1] == 0 for w in quiet), str([w[1] for w in quiet]))
check("🔴 [회귀] 그래서 5분을 흘려도 매수 자격을 잃지 않는다",
      all(w[0] for w in quiet), str([w[0] for w in quiet]))
check("🔴 [회귀] 읽기전용 카운터가 값을 올리지 않는다",
      s5q._burst_wave_count("QUIET") == 0
      and s5q._burst_wave_count("QUIET") == 0)

# 09:05 이후(가속 경로)는 예전과 동일하게 발사마다 센다 — 면제가 아니므로
# '무조건 참'이 아니고, 가속 성립 자체가 거래대금 집중 이벤트다.
s5a = build(datetime(2026, 8, 10, 9, 6, 0))
setup(s5a, "ACC")
arm(s5a, "ACC", NOW - 10.0)
_tfa = s5a.phase1b.trade_flow
feed_uniform(_tfa, "ACC", 40_000_000, NOW - 30.0, span=90.0)   # 앞 90초
feed_uniform(_tfa, "ACC", 90_000_000, NOW, span=29.0)          # 최근 30초 집중
_o, _d = s5a.evaluate_tick_entry(
    "ACC", "1A", 10_000, open_price=10_000, cond_name="주도주상위",
    now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0))
check("[H] 09:05 이후 가속 발사는 그대로 파동 1을 센다",
      _o is True and _d.get("burst_wave") == 1,
      f"발사={_o} 파동={_d.get('burst_wave')}")

# 롤백 — False로 두면 08-08 사양(게이트 통과마다 카운트)이 그대로 돌아온다
_saved_wc = SM.BURST_WAVE_COUNT_REQUIRES_BURST
try:
    SM.BURST_WAVE_COUNT_REQUIRES_BURST = False
    s5r = build(datetime(2026, 8, 10, 9, 2, 0))
    setup(s5r, "RB")
    arm(s5r, "RB", NOW - 10.0)
    rb = []
    for i in range(SM.BURST_WAVE_MAX + 1):
        t = NOW + i * (SM.BURST_WAVE_COOLDOWN_SEC + 1)
        o, d = s5r.evaluate_tick_entry(
            "RB", "1A", 10_000, open_price=10_000, cond_name="주도주상위",
            now=t, now_dt=datetime(2026, 8, 10, 9, 2, 0))
        rb.append((o, d.get("burst_wave")))
    check("[롤백] False면 폭발 없이도 파동이 쌓인다(08-08 사양)",
          [w[1] for w in rb] == [1, 2, 3, 4] and rb[-1][0] is False,
          str([w[1] for w in rb]))
finally:
    SM.BURST_WAVE_COUNT_REQUIRES_BURST = _saved_wc
check("[롤백 후] 정상 복원", SM.BURST_WAVE_COUNT_REQUIRES_BURST is True)

# 지수 HALT는 발사보다 먼저 걸린다
s5b = build(datetime(2026, 8, 10, 9, 2, 0))
setup(s5b, "HHH")
arm(s5b, "HHH", NOW - 10.0)
s5b._market_defense_mode = "HALT"
try:
    s5b._get_market_defense_mode = lambda: "HALT"
except Exception:
    pass
oh, dh = s5b.evaluate_tick_entry(
    "HHH", "1A", 10_000, open_price=10_000, cond_name="주도주상위",
    now=NOW, now_dt=datetime(2026, 8, 10, 9, 2, 0),
)
check("면제 구간이어도 지수 HALT가 먼저 막는다", oh is False, dh.get("reason", "")[:50])

# 무장 미달이면 면제 구간이어도 발사되지 않는다
s5c = build(datetime(2026, 8, 10, 9, 2, 0))
setup(s5c, "III")
oc, dc = s5c.evaluate_tick_entry(
    "III", "1A", 10_000, open_price=10_000, cond_name="주도주상위",
    now=NOW, now_dt=datetime(2026, 8, 10, 9, 2, 0),
)
check("면제는 '발사'만이다 — 무장 미달은 그대로 막힌다",
      oc is False and "미무장" in dc.get("reason", ""), dc.get("reason", "")[:60])

# 시가대비 상한도 그대로
s5d = build(datetime(2026, 8, 10, 9, 2, 0))
setup(s5d, "JJJ", open_px=10_000)
arm(s5d, "JJJ", NOW - 10.0, price=11_000)
od, dd = s5d.evaluate_tick_entry(
    "JJJ", "1A", 11_000, open_price=10_000, cond_name="주도주상위",
    now=NOW, now_dt=datetime(2026, 8, 10, 9, 2, 0),
)
check("면제 구간에서도 시가대비 상한(+8%)은 유효",
      od is False and "시가대비" in dd.get("reason", ""), dd.get("reason", "")[:60])

# ═════════════════════════════════════════════════════════
print("\n[6] check_burst는 그대로 남아 있는가 (재매수·슬롯교체 경로 불변)")
# ═════════════════════════════════════════════════════════
s6 = build(datetime(2026, 8, 10, 9, 6, 0))
setup(s6, "KKK")
calls = []
_orig = s6.check_burst
def _spy(code, **kw):
    calls.append(kw)
    return _orig(code, **kw)
s6.check_burst = _spy
s6.sold_at["KKK"] = s6._now().replace(hour=8, minute=0)
s6._rebuy_after_loss_used.pop("KKK", None)
# ⚠️ 메서드명은 추측하지 말고 grep으로 확인한 실명을 쓴다(08-06 교훈).
s6._rebuy_after_loss_ok("KKK")
check("재매수 판정이 여전히 check_burst를 쓴다(가속도로 갈아끼우지 않았다)",
      len(calls) >= 1, f"호출 {len(calls)}회 {calls[:1]}")
check("재매수는 상대 경로 금지 + 대금 배수를 그대로 넘긴다",
      bool(calls) and calls[0].get("allow_relative") is False
      and calls[0].get("value_mult") == SM.REBUY_BURST_VALUE_MULT,
      str(calls[:1]))

import core.slot_replacement as SR
check("슬롯 교체도 check_burst를 그대로 쓴다",
      "check_burst" in open(SR.__file__, encoding="utf-8").read())

# ═════════════════════════════════════════════════════════
print("\n[7] 롤백 — FIRE_GATE_SPLIT_ENABLED=False면 08-07 동작으로 복귀")
# ═════════════════════════════════════════════════════════
_saved = SM.FIRE_GATE_SPLIT_ENABLED
try:
    SM.FIRE_GATE_SPLIT_ENABLED = False
    s7 = build(datetime(2026, 8, 10, 9, 2, 0))
    setup(s7, "LLL")
    o7, d7 = s7._fire_gate("LLL", now=NOW, now_dt=datetime(2026, 8, 10, 9, 2, 0))
    check("[롤백] 09:02에도 면제되지 않는다(버스트로 판정)",
          o7 is False and "대량체결 부족" in d7.get("reason", ""),
          d7.get("reason", "")[:60])

    # 버스트가 성립하면 09:02에도 통과 — 즉 완전한 옛 동작
    tf7 = s7.phase1b.trade_flow
    for i in range(SM.PHASE1A_BURST_TRADE_COUNT):
        tf7.add_tick("LLL", 10_000, "buy",
                     int(SM.PHASE1A_BURST_TRADE_VALUE * scale * 1.05 // 10_000),
                     now=NOW - 1.0 - i * 0.3)
    o7b, d7b = s7._fire_gate("LLL", now=NOW, now_dt=datetime(2026, 8, 10, 9, 2, 0))
    check("[롤백] 버스트가 성립하면 통과(옛 경로 그대로)",
          o7b is True and d7b.get("burst_path") in ("절대", "단일", "상대"),
          str(d7b.get("burst_path")))

    # 09:06에도 가속이 아니라 버스트를 본다
    o7c, d7c = s7._fire_gate("LLL", now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0))
    check("[롤백] 09:06에도 가속이 아니라 버스트로 판정",
          o7c is True and d7c.get("fire_gate") is None, str(d7c.get("burst_path")))
finally:
    SM.FIRE_GATE_SPLIT_ENABLED = _saved
check("[롤백] 플래그가 원복됐다", SM.FIRE_GATE_SPLIT_ENABLED is True)

# 전환 시각을 09:00으로 두면 '전 구간 가속도'가 된다(튜닝 여지 확인)
_savedt = SM.FIRE_GATE_ACCEL_FROM
try:
    SM.FIRE_GATE_ACCEL_FROM = dtime(9, 0)
    s7d = build(datetime(2026, 8, 10, 9, 2, 0))
    setup(s7d, "MMM")
    o7d, d7d = s7d._fire_gate("MMM", now=NOW, now_dt=datetime(2026, 8, 10, 9, 2, 0))
    check("[튜닝] 전환시각 09:00 -> 초반에도 가속 판정",
          o7d is False and d7d.get("fire_gate") == "accel")
finally:
    SM.FIRE_GATE_ACCEL_FROM = _savedt
check("[튜닝] 전환시각이 원복됐다", SM.FIRE_GATE_ACCEL_FROM == dtime(9, 5))

# ═════════════════════════════════════════════════════════
print("\n[8] 개장 초반 슬롯 캡 — 발사 면제의 짝")
# ═════════════════════════════════════════════════════════
check("초반 캡 ON", SM.EARLY_SLOT_CAP_ENABLED is True)
check("캡 4", SM.EARLY_SLOT_CAP == 4, str(SM.EARLY_SLOT_CAP))
check("적용 종료가 발사 전환 시각과 같다",
      SM.EARLY_SLOT_CAP_UNTIL == SM.FIRE_GATE_ACCEL_FROM,
      f"{SM.EARLY_SLOT_CAP_UNTIL} / {SM.FIRE_GATE_ACCEL_FROM}")
# 🔴 캡이 공유 상한 이상이면 아무것도 제한하지 못한다(있으나 마나).
check("캡 < 공유 상한 (실효성 있는 제한)",
      SM.EARLY_SLOT_CAP < SM.MAX_HOLDINGS,
      f"{SM.EARLY_SLOT_CAP} < {SM.MAX_HOLDINGS}")
check("캡 >= 1 (0이면 초반에 아무것도 못 산다)", SM.EARLY_SLOT_CAP >= 1)


def _fill(strat, n, kind="holding"):
    """슬롯을 n칸 채운다(보유/되돌림대기 어느 쪽이든 occupied_slots가 센다).

    ⚠️ 포지션 dict의 키는 실물과 맞춘다 — `buy_quantity`가 없으면
    `_ensure_base_capital`이 KeyError로 죽는다(스텁이 실물과 다르면 검증
    자체가 거짓말을 한다는 08-05 감사 교훈).
    """
    for i in range(n):
        c = f"FILL{i}"
        if kind == "holding":
            strat.holdings[c] = {"sub_strategy": "1A", "buy_price": 10_000,
                                 "buy_quantity": 1, "quantity": 1,
                                 "buy_time": strat._now()}
        else:
            strat._entry_plans[c] = {"trigger_price": 10_000, "targets": [],
                                     "deadline": 0, "sub_strategy": "1A",
                                     "info": {}, "cond_name": "", "phase": 1,
                                     "stock_name": c}


s8a = build(datetime(2026, 8, 10, 9, 2, 0))
_fill(s8a, SM.EARLY_SLOT_CAP - 1)
check(f"[경계] 09:02 · {SM.EARLY_SLOT_CAP - 1}칸이면 통과",
      s8a.can_buy_more({}, "1A") is True, f"occupied={s8a.occupied_slots()}")
s8b = build(datetime(2026, 8, 10, 9, 2, 0))
_fill(s8b, SM.EARLY_SLOT_CAP)
check(f"[경계] 09:02 · {SM.EARLY_SLOT_CAP}칸이면 차단",
      s8b.can_buy_more({}, "1A") is False, f"occupied={s8b.occupied_slots()}")
check("차단 사유가 '개장초반 슬롯 캡'으로 분류된다",
      SM.StrategyManager._reject_category(s8b.early_slot_cap_reject())
      == "개장초반 슬롯 캡", str(s8b.early_slot_cap_reject()))
check("'슬롯 부족'과 뭉개지지 않는다",
      SM.StrategyManager._reject_category(s8b.early_slot_cap_reject()) != "슬롯 부족")

# 되돌림 대기(계획)도 캡에 센다 — 계획이 슬롯을 120초 점유하기 때문
s8c = build(datetime(2026, 8, 10, 9, 2, 0))
_fill(s8c, SM.EARLY_SLOT_CAP, kind="plan")
check("되돌림 대기 계획도 캡에 포함된다",
      s8c.can_buy_more({}, "1A") is False, f"occupied={s8c.occupied_slots()}")

# 09:05부터는 캡이 풀리고 원래 상한(6)으로 돌아간다
s8d = build(datetime(2026, 8, 10, 9, 5, 0))
_fill(s8d, SM.EARLY_SLOT_CAP)
check("[경계] 09:05:00부터 캡 해제 — 공유 상한까지 쓴다",
      s8d.early_slot_cap_reject() is None and s8d.can_buy_more({}, "1A") is True)
s8e = build(datetime(2026, 8, 10, 9, 5, 0))
_fill(s8e, SM.MAX_HOLDINGS)
check("09:05 이후에도 공유 상한 자체는 그대로 유효",
      s8e.can_buy_more(None, "1A") is False, f"occupied={s8e.occupied_slots()}")

# 🔴 초반에는 확장 슬롯(7~8)이 열리면 안 된다 — 캡의 의미가 사라진다
s8f = build(datetime(2026, 8, 10, 9, 2, 0))
_fill(s8f, SM.MAX_HOLDINGS)
s8f._soft_cap_full_since = s8f._now() - timedelta(seconds=9_999)
check("초반엔 확장 슬롯도 열리지 않는다(캡이 확장보다 먼저)",
      s8f.can_buy_more({"score": 99.0, "score_threshold": 1.0}, "1A") is False)

# 눌림목(슬롯 0)과 무관하게 1A 경로에 걸린다 = 전략 공통
s8g = build(datetime(2026, 8, 10, 9, 2, 0))
_fill(s8g, SM.EARLY_SLOT_CAP)
check("캡은 전략 무관(공통 창구 can_buy_more)",
      s8g.can_buy_phase1a({}) is False and s8g.can_buy_pullback({}) is False)

# 실제 진입 경로에서도 막히는가 — 계획이 열리지 않아야 한다
s8h = build(datetime(2026, 8, 10, 9, 2, 0))
_fill(s8h, SM.EARLY_SLOT_CAP)
setup(s8h, "CAPPED")
arm(s8h, "CAPPED", NOW - 10.0)
s8h.on_trade({"stock_code": "CAPPED", "price": 10_000, "side": "buy",
              "volume": 10, "strength": 130.0}, now=NOW)
check("[통합] 캡에 걸리면 되돌림 계획 자체가 안 열린다",
      "CAPPED" not in s8h._entry_plans and "CAPPED" not in s8h.holdings)

# 롤백
_savedc = SM.EARLY_SLOT_CAP_ENABLED
try:
    SM.EARLY_SLOT_CAP_ENABLED = False
    s8i = build(datetime(2026, 8, 10, 9, 2, 0))
    _fill(s8i, SM.EARLY_SLOT_CAP)
    check("[롤백] 캡을 끄면 09:02에도 공유 상한까지 쓴다",
          s8i.early_slot_cap_reject() is None and s8i.can_buy_more({}, "1A") is True)
finally:
    SM.EARLY_SLOT_CAP_ENABLED = _savedc
check("[롤백] 플래그 원복", SM.EARLY_SLOT_CAP_ENABLED is True)

# ═════════════════════════════════════════════════════════
print("\n[9] VWAP 사각지대 — 검사 가격 == 실제 지불 가격")
# ═════════════════════════════════════════════════════════
check("깊은 트랜치 검사 ON", SM.VWAP_ENTRY_CHECK_DEEPEST is True)
_deep = max(d for d, _ in SM.ENTRY_PULLBACK_TRANCHES)
check("가장 깊은 트랜치 = 되돌림 최대 깊이", abs(_deep - 0.007) < 1e-9, str(_deep))


def _plan_case(gap_pct):
    """트리거가가 세션VWAP보다 gap_pct% 위인 상황에서 계획이 열리는가."""
    st = build(datetime(2026, 8, 10, 9, 6, 0))
    setup(st, "VW")
    st._session_vwap["VW"] = 10_000.0
    trig = 10_000.0 * (1 + gap_pct / 100.0)
    st._prev_closes["VW"] = trig * 0.95            # 등락률 게이트는 통과시킨다
    st._open_entry_plan("VW", "VW", 1, {"score": 1.0, "score_threshold": 1.0},
                        "1A", "주도주상위", trigger_price=trig, now=NOW)
    return st, trig


# 죽은 구간(트리거 +0.6%) — 예전엔 계획이 열려 슬롯만 120초 묶었다.
s9a, _t9a = _plan_case(0.6)
check("[사각지대] 트리거 +0.6%면 계획이 아예 안 열린다(슬롯 낭비 방지)",
      "VW" not in s9a._entry_plans)
check("[사각지대] 그 자리의 최저 체결가는 VWAP 게이트를 못 넘는다",
      s9a.vwap_entry_reject("VW", _t9a * (1 - _deep)) is not None)

# 충분히 위(트리거 +1.5%)면 계획이 열리고 **두 트랜치 모두** 체결 가능
s9c, trig_c = _plan_case(1.5)
check("[정상] 트리거 +1.5%면 계획이 열린다", "VW" in s9c._entry_plans)
for _d, _f in SM.ENTRY_PULLBACK_TRANCHES:
    check(f"[정상] -{_d*100:.1f}% 트랜치가도 VWAP 통과(반쪽 포지션 없음)",
          s9c.vwap_entry_reject("VW", trig_c * (1 - _d)) is None,
          f"{trig_c * (1 - _d):,.0f}원")

_sv = SM.VWAP_ENTRY_CHECK_DEEPEST
try:
    SM.VWAP_ENTRY_CHECK_DEEPEST = False
    s9d, _ = _plan_case(0.6)
    check("[롤백] 끄면 트리거 +0.6%에서도 계획이 열린다(옛 죽은 구간 재현)",
          "VW" in s9d._entry_plans)
finally:
    SM.VWAP_ENTRY_CHECK_DEEPEST = _sv
check("[롤백] 플래그 원복", SM.VWAP_ENTRY_CHECK_DEEPEST is True)

_se = SM.VWAP_ENTRY_ENABLED
try:
    SM.VWAP_ENTRY_ENABLED = False
    s9e, _ = _plan_case(0.0)
    check("[교차] VWAP OFF면 깊은 검사도 무해(계획 정상 생성)",
          "VW" in s9e._entry_plans)
finally:
    SM.VWAP_ENTRY_ENABLED = _se

# ═════════════════════════════════════════════════════════
print("\n[10] 우선순위 교체 일일 한도 (초반 캡의 부작용 차단)")
# ═════════════════════════════════════════════════════════
check("한도 상수 존재", hasattr(SM, "PHASE1A_PRIORITY_MAX_PER_DAY"))
# (2026-08-10) 원래 `> 0`이었다. 0은 상수 주석이 명시한 **정상 상태**('완전 OFF')이고
# 오늘 실제로 0으로 껐다 — 09:01:41~09:03:36에 6회를 전부 소진하고 그중 3회를
# 유령 포지션에 낭비했다. 음수만 막는다.
check("한도 >= 0 (0 = 교체 완전 OFF, 정상 상태)",
      SM.PHASE1A_PRIORITY_MAX_PER_DAY >= 0, str(SM.PHASE1A_PRIORITY_MAX_PER_DAY))
check("한도 <= 공유 슬롯 수(슬롯당 평균 1회)",
      SM.PHASE1A_PRIORITY_MAX_PER_DAY <= SM.MAX_HOLDINGS)
check("카운터 초기값 0", build()._priority_upgrades_today == 0)

# ═════════════════════════════════════════════════════════
print("\n[11] 핫패스 비용 — 발사 판정은 매 틱 돈다")
# ═════════════════════════════════════════════════════════
s8 = build(datetime(2026, 8, 10, 9, 6, 0))
setup(s8, "NNN")
feed_uniform(s8.phase1b.trade_flow, "NNN", 500_000_000, NOW, span=120.0, n=400)
t0 = time.perf_counter()
for _ in range(300):
    s8._fire_gate("NNN", now=NOW, now_dt=datetime(2026, 8, 10, 9, 6, 0))
per_ms = (time.perf_counter() - t0) / 300 * 1000
check(f"_fire_gate 1회 {per_ms:.3f}ms < 5ms", per_ms < 5.0, f"{per_ms:.3f}ms")

# ═════════════════════════════════════════════════════════
print("\n" + "=" * 66)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
if FAIL:
    for f in FAIL:
        print(f"  - {f}")
print("=" * 66)
sys.exit(1 if FAIL else 0)
