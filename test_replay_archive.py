# -*- coding: utf-8 -*-
"""틱 아카이브 통합 리플레이 — 실제 StrategyManager에 실제 틱을 관통시킨다.

🔴 **이 파일이 존재하는 이유**
다른 스위트는 전부 **사양**을 본다("이 게이트가 이 입력을 막는가"). 이 파일은
`cache/tick_history/`에 쌓인 **그날의 진짜 체결틱**을 실물 StrategyManager에
그대로 흘려, "실제로 무슨 일이 일어나는가"를 본다. 사양 테스트가 전원 통과해도
배선이 어긋나 매수 0건이 되거나, 규칙이 조용히 무효가 되는 일이 이 프로젝트에서
반복해서 일어났다(2026-08-09 buy_price=1 / 2026-08-10 compute_stop_rate 창 비움).

⚠️ **08-08, 08-09, 08-10 세 세션이 이걸 매번 스크래치패드에 새로 만들었다가
잃어버렸다.** 그래서 이번엔 커밋한다(이월과제 #3).

────────────────────────────────────────────────────────────────
재현되는 것 / 안 되는 것
────────────────────────────────────────────────────────────────
✅ 체결틱(가격·수량·side) / 세션 VWAP(FID13·14 누적) / 전일종가 / 시가 /
   숙성 / 등락률 / 파동 / 되돌림 분할 / 슬롯 / 초반 캡 / 청산 전 규칙
❌ **무장(FID228)은 아카이브에 없다** — strength=130 고정으로 **항상 무장**시킨다.
   즉 이 리플레이는 **최악 케이스**(실전보다 훨씬 많이 산다)다.
   건수의 절대값이 아니라 **불변식 위반 여부**만 읽을 것.

🔴 **아카이브 폴더명 != 실제 틱 날짜.**
   아카이버(`core/tick_archive.py`)는 ka10079로 소급 수집하는데, 당일 거래가
   없던 종목은 **직전 거래일 틱**이 그대로 돌아와 오늘 폴더에 저장된다.
   실측: `20260810/` 37개 파일 중 실제 08-10 틱은 12개뿐이다.
   -> **반드시 틱의 time_str로 날짜를 다시 묶는다.** 폴더명으로 묶으면
      "4일치 분석"이 실제로는 7개 날짜가 섞인 것이 된다.

실행: PYTHONIOENCODING=utf-8 AUTOTRADER_TEST_LOG=1 python test_replay_archive.py
"""
import os
import sys

os.environ.setdefault("AUTOTRADER_TEST_LOG", "1")   # core 임포트보다 먼저

import glob
import json
import time as _t
from collections import defaultdict
from datetime import datetime, time as dtime

import core.strategy_manager as SM                      # noqa: E402
from core.phase1b_controller import Phase1BController    # noqa: E402

PASS, FAIL = [], []
T0 = _t.time()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(BASE_DIR, "cache", "tick_history")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} | {name}{(' -- ' + detail) if detail else ''}")


def section(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


# ─────────────────────────────────────────────────────────
# 스텁 — 실물과 **같은 형식**으로 돌려준다.
# ⚠️ 스텁이 실물과 다르면 리플레이 자체가 거짓말을 한다. 이 프로젝트가
#    실제로 두 번 당했다:
#     · update_sell의 trade_id가 실물은 **위치 인자**인데 스텁이 키워드
#       전용이라 TypeError -> 호출부 except가 삼켜 'DB 종료 0건'
#     · _OrderMgr.buy가 시장가(price=0)에 1원을 돌려줘 buy_price=1 ->
#       가격 위생검사가 전 종목 청산을 스킵해 '매도 0건'
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
    def update_sell(cls, trade_id=None, sell_price=0, sell_quantity=0,
                    exit_reason=None, **kw):
        # 실물: update_sell(trade_id, sell_price, sell_quantity, exit_reason=..., ...)
        cls.sells.append({"trade_id": trade_id, "sell_price": sell_price,
                          "sell_quantity": sell_quantity,
                          "exit_reason": exit_reason, **kw})
        return True

    @classmethod
    def update(cls, rid, data): cls.updates.append({"id": rid, **data}); return True

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
    """진입 핫패스에서 **한 번도 불리면 안 되는** REST. 불리면 calls에 쌓인다."""
    host = "https://mock"

    def __init__(self):
        self.calls = []

    def get_minute_candles(self, code, interval=1, count=1, base_date=None):
        self.calls.append(("candles", code)); return []

    def get_daily_candles(self, code, count=30, base_date=None):
        self.calls.append(("daily", code)); return []

    def get_orderable_amount(self):
        self.calls.append(("cash", None)); return 10_000_000

    def get_stock_change_rate(self, code):
        self.calls.append(("chg", code)); return 3.0

    def get_basic_quote(self, code):
        self.calls.append(("quote", code)); return {"change_rate": 3.0}

    def get_index_change_rate(self, s="001"): return 0.0

    def get_current_price(self, code):
        self.calls.append(("price", code)); return 0


class _OrderMgr:
    """⚠️ 시장가(price=0)면 실물은 **ref_price(매도1호가)**를 돌려준다.
    여기서 1원 같은 걸 돌려주면 buy_price=1이 되어 청산 판정이 전부 스킵된다."""

    def __init__(self):
        self.orders = []
        self.last_px = {}

    def buy(self, code, qty, price=0, sizing="REGULAR", exit_strategy="REGULAR",
            order_style="limit", ref_price=0):
        px = price or ref_price or self.last_px.get(code) or 0
        self.orders.append({"side": "buy", "code": code, "qty": qty,
                            "style": order_style, "price": px})
        return {"success": True, "ord_no": "1", "price": px, "style": order_style}

    def sell(self, code, qty, price=0, order_style="market"):
        px = price or self.last_px.get(code) or 0
        self.orders.append({"side": "sell", "code": code, "qty": qty,
                            "style": order_style, "price": px})
        return {"success": True, "ord_no": "2", "price": px, "style": order_style}

    def get_stock_name(self, code): return code


# ─────────────────────────────────────────────────────────
# 아카이브 로딩 — **실제 틱 날짜**로 다시 묶는다
# ─────────────────────────────────────────────────────────
def load_archive():
    """{'YYYYMMDD': {code: [(epoch, price, side, volume), ...]}}"""
    by_date = defaultdict(lambda: defaultdict(list))
    seen = set()   # (실제날짜, 종목) — 같은 종목이 여러 폴더에 중복 저장돼 있다
    for path in sorted(glob.glob(os.path.join(ARCHIVE_DIR, "*", "*.json"))):
        code = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, encoding="utf-8") as f:
                ticks = json.load(f).get("ticks") or []
        except Exception:
            continue
        if not ticks:
            continue
        day = str(ticks[0].get("time_str", ""))[:8]
        if len(day) != 8 or (day, code) in seen:
            continue
        seen.add((day, code))
        out = []
        for tk in ticks:
            ts = str(tk.get("time_str") or "")
            if len(ts) != 14 or ts[:8] != day:
                continue          # 날짜가 섞인 틱은 버린다
            try:
                dt = datetime.strptime(ts, "%Y%m%d%H%M%S")
                px = int(tk.get("price") or 0)
                vol = int(tk.get("volume") or 0)
            except (ValueError, TypeError):
                continue
            if px <= 0 or vol <= 0:
                continue
            out.append((dt, px, str(tk.get("side") or "neutral"), vol))
        if len(out) >= 50:
            out.sort(key=lambda r: r[0])
            by_date[day][code] = out
    return by_date


# ─────────────────────────────────────────────────────────
# 하루치 리플레이
# ─────────────────────────────────────────────────────────
class Result:
    def __init__(self, day):
        self.day = day
        self.buys = []
        self.sells = []
        self.exceptions = []
        self.max_slots = 0
        self.max_slots_early = 0
        self.stocks = 0
        self.ticks = 0
        self.rest_calls = 0
        self.stop_rates = {}


def replay_day(day, per_code, vol_stop=True):
    """하루치를 실물 StrategyManager에 관통시킨다.

    ⚠️ 시각 처리가 이 하네스의 가장 미묘한 부분이다.
      · `now_dt`(self._now())는 **그날의 시뮬레이션 시각**이어야 시간창·숙성·
        초반 캡·워밍업이 진짜로 돈다.
      · 반면 실물 일부는 `now` 인자 없이 `time.time()`을 직접 쓴다. 가상 epoch를
        주면 "숙성 미달"로 전부 막혀 **매수 0건**이 된다(08-08에 실제로 속았다).
      -> 그래서 wall clock은 **실시간 기준 과거 시각**으로 매핑한다.
    """
    sv_vol = SM.STOP_LOSS_VOL_ENABLED
    SM.STOP_LOSS_VOL_ENABLED = vol_stop

    SM.TradeRepository = _Repo
    SM.WatchListRepository = _Repo
    SM.SystemEventRepository = _Repo
    SM.ThemeManager = _Theme
    SM.send_telegram = None
    _Repo.rows, _Repo.sells, _Repo.updates = [], [], []

    res = Result(day)
    rest, omgr = _Rest(), _OrderMgr()

    # 전 종목 틱을 **하나의 시간축**으로 합친다 — 슬롯 경쟁이 실제로 일어난다.
    merged = []
    for code, ticks in per_code.items():
        for dt, px, side, vol in ticks:
            merged.append((dt, code, px, side, vol))
    if not merged:
        SM.STOP_LOSS_VOL_ENABLED = sv_vol
        return res
    merged.sort(key=lambda r: r[0])

    t_first, t_last = merged[0][0], merged[-1][0]
    span = max(1.0, (t_last - t_first).total_seconds())
    WALL0 = _t.time() - span - 300.0        # 전부 과거가 되도록

    holder = {"dt": t_first}
    strat = SM.StrategyManager(
        kiwoom_rest=rest, order_manager=omgr,
        phase1b_controller=Phase1BController(), portfolio_optimizer=None,
        now_func=lambda: holder["dt"],
    )

    # ── pre-arm: 조건검색 편입을 흉내낸다(REST 없이 캐시를 직접 채운다) ──
    for code, ticks in per_code.items():
        first_px = ticks[0][1]
        strat._stock_names[code] = code
        strat._cond_names[code] = "주도주상위"
        strat.watch_list_today.add(code)
        strat._opening_prices[code] = float(first_px)
        # 전일종가를 시가보다 1% 아래로 둔다 -> 등락률 하한(0% 초과) 통과.
        strat._prev_closes[code] = float(first_px) / 1.01
        strat._first_seen[code] = WALL0 - 999.0     # 숙성 통과
        strat.phase1b.start_watching(code)
        strat.phase1b.orderbook.update(
            code,
            {"ask_prices": [first_px, first_px + 10, first_px + 20],
             "ask_volumes": [5_000, 5_000, 5_000]},
            now=WALL0,
        )

    acc_vol = defaultdict(int)      # FID 13 누적거래량
    acc_val = defaultdict(float)    # FID 14 누적거래대금(백만원)
    n_buy_before = 0

    for dt, code, px, side, vol in merged:
        holder["dt"] = dt
        wall = WALL0 + (dt - t_first).total_seconds()
        omgr.last_px[code] = px

        acc_vol[code] += vol
        acc_val[code] += px * vol / 1_000_000.0

        parsed = {
            "stock_code": code, "price": px, "side": side, "volume": vol,
            "strength": 130.0,                    # ⚠️ 항상 무장 = 최악 케이스
            "raw": {SM.VWAP_FID_ACC_VOLUME: str(acc_vol[code]),
                    SM.VWAP_FID_ACC_VALUE: str(int(acc_val[code]))},
        }
        try:
            strat.on_trade(parsed, now=wall)
        except Exception as e:      # noqa: BLE001
            res.exceptions.append(f"{code} {dt} {type(e).__name__}: {e}")

        # 매수가 새로 잡혔으면 그 시점 사실을 기록해 사후 검증한다.
        if len(omgr.orders) != n_buy_before:
            for o in omgr.orders[n_buy_before:]:
                rec = dict(o)
                rec.update({"dt": dt, "tick_px": px, "code": o["code"]})
                if o["side"] == "buy":
                    pos = strat.holdings.get(o["code"]) or {}
                    rec["stop_rate"] = pos.get("stop_rate")
                    rec["buy_price"] = pos.get("buy_price")
                    res.buys.append(rec)
                else:
                    res.sells.append(rec)
            n_buy_before = len(omgr.orders)

        occ = strat.occupied_slots()
        res.max_slots = max(res.max_slots, occ)
        if dt.time() < SM.EARLY_SLOT_CAP_UNTIL:
            res.max_slots_early = max(res.max_slots_early, occ)

    for code, pos in strat.holdings.items():
        res.stop_rates[code] = pos.get("stop_rate")

    res.stocks = len(per_code)
    res.ticks = len(merged)
    res.rest_calls = len(rest.calls)
    # 종목별/진입별 REST와 '세션 1회' REST를 구분한다.
    # `_ensure_base_capital`(MDD 기준자본)은 설계상 하루 1회이고 캐시된다 —
    # 이걸 위반으로 세면 진짜 위반(종목마다 부르는 것)이 묻힌다.
    res.rest_percall = [c for c in rest.calls if c[0] != "cash"]
    res.rest_cash = [c for c in rest.calls if c[0] == "cash"]
    res.sell_reasons = [s.get("exit_reason") for s in _Repo.sells]
    SM.STOP_LOSS_VOL_ENABLED = sv_vol
    return res


# ══════════════════════════════════════════════════════════
section("[0] 아카이브 로딩 — 폴더명이 아니라 실제 틱 날짜로 묶는다")
# ══════════════════════════════════════════════════════════
DATA = load_archive()
days = sorted(DATA.keys())
check("아카이브가 존재한다", bool(days), f"{len(days)}일: {', '.join(days)}")

n_folder = len(glob.glob(os.path.join(ARCHIVE_DIR, "*", "*.json")))
n_real = sum(len(v) for v in DATA.values())
print(f"       파일 {n_folder}개 -> 실제 (날짜,종목) {n_real}개 / {len(days)}일")
check("폴더 수(6)보다 실제 날짜가 많다 — 폴더명 신뢰 금지의 증거",
      len(days) > len(glob.glob(os.path.join(ARCHIVE_DIR, "*"))),
      f"폴더 {len(glob.glob(os.path.join(ARCHIVE_DIR, '*')))}개 vs 날짜 {len(days)}일")

if not days:
    print("\n아카이브가 없어 리플레이를 건너뜁니다.")
    sys.exit(1)

# ══════════════════════════════════════════════════════════
section("[1] 전 일자 리플레이 — 예외 0 / REST 0 / 슬롯 불변식")
# ══════════════════════════════════════════════════════════
results = []
print(f"{'날짜':>10} {'종목':>4} {'틱':>7} {'매수':>4} {'매도':>4} "
      f"{'초반최대':>8} {'전체최대':>8} {'예외':>4} {'REST':>5}")
for day in days:
    r = replay_day(day, DATA[day])
    results.append(r)
    print(f"{day:>10} {r.stocks:>4} {r.ticks:>7} {len(r.buys):>4} {len(r.sells):>4} "
          f"{r.max_slots_early:>8} {r.max_slots:>8} {len(r.exceptions):>4} "
          f"{r.rest_calls:>5}")

tot_buy = sum(len(r.buys) for r in results)
tot_sell = sum(len(r.sells) for r in results)
tot_exc = sum(len(r.exceptions) for r in results)
tot_rest = sum(r.rest_calls for r in results)

check("예외 0건", tot_exc == 0,
      "; ".join(results[0].exceptions[:2]) if tot_exc else "")

# 🔴 '진입 핫패스 REST 0콜'의 정확한 의미 — 종목·진입마다 부르는 REST가 0이라는
# 것이지, 프로세스가 REST를 한 번도 안 쓴다는 뜻이 아니다. MDD 기준자본
# (`_ensure_base_capital` -> get_orderable_amount)은 설계상 **세션 1회**이고
# `_base_capital` 캐시로 수렴한다(실측: 144,905틱에 1콜).
# 둘을 뭉개면 진짜 위반(캐시 미스로 종목마다 부르는 것)이 이 1콜에 묻힌다.
tot_percall = sum(len(r.rest_percall) for r in results)
bad_cash = [(r.day, len(r.rest_cash)) for r in results if len(r.rest_cash) > 1]
check("진입 핫패스에 종목별 REST 0콜 (분봉/일봉/현재가/등락률/호가)",
      tot_percall == 0, f"{tot_percall}콜")
check("MDD 기준자본 조회는 하루 1회로 캐시된다", not bad_cash, str(bad_cash))
check("매수가 실제로 발생한다(배선이 죽지 않았다)", tot_buy > 0, f"{tot_buy}건")
check("매도가 실제로 발생한다(청산이 죽지 않았다)", tot_sell > 0, f"{tot_sell}건")

# ══════════════════════════════════════════════════════════
section("[2] 슬롯 불변식 — 초반 캡 4 / 공유 상한 6")
# ══════════════════════════════════════════════════════════
bad_early = [r.day for r in results if r.max_slots_early > SM.EARLY_SLOT_CAP]
bad_hard = [r.day for r in results if r.max_slots > SM.MAX_HOLDINGS]
check(f"09:05 이전 동시점유 <= {SM.EARLY_SLOT_CAP} (전 일자)",
      not bad_early, f"위반 {bad_early}")
check(f"전 구간 동시점유 <= 공유상한 {SM.MAX_HOLDINGS}",
      not bad_hard, f"위반 {bad_hard}")

# ══════════════════════════════════════════════════════════
section("[3] 매수 위생 — 체결단가가 진짜 가격인가")
# ══════════════════════════════════════════════════════════
# 🔴 08-09에 스텁이 1원을 돌려줘 buy_price=1이 됐고, 그 결과 가격 위생검사가
#    전 종목 청산을 스킵해 '매도 0건'이라는 가짜 결과가 나왔다.
bad_px, bad_qty = [], []
for r in results:
    for b in r.buys:
        bp = b.get("buy_price") or b.get("price") or 0
        if not bp or abs(bp - b["tick_px"]) / b["tick_px"] > 0.10:
            bad_px.append((r.day, b["code"], bp, b["tick_px"]))
        if not b.get("qty") or b["qty"] <= 0:
            bad_qty.append((r.day, b["code"], b.get("qty")))
check("전 매수의 체결단가가 틱가의 ±10% 이내", not bad_px, str(bad_px[:3]))
check("전 매수의 수량 > 0", not bad_qty, str(bad_qty[:3]))

# ══════════════════════════════════════════════════════════
section("[4] 매도 위생 — 사유 없는 매도가 없는가")
# ══════════════════════════════════════════════════════════
ALLOWED = ("손절", "지수 가드", "VI 상단", "본전스톱", "분할 잔량 트레일",
           "익절", "정체 정리", "시간정리", "반등소진", "동적캡", "강제청산",
           "추가매수 후 최종손절", "우선순위", "슬롯 교체", "미체결 포지션 정리")
reasons = [x for r in results for x in getattr(r, "sell_reasons", [])]
unknown = [x for x in reasons if not x or not any(a in x for a in ALLOWED)]
check("모든 DB 청산에 정당한 사유가 있다", not unknown,
      f"{len(unknown)}건 {unknown[:2]}")

# 🔴 08-10에 정책으로 끈 것들이 정말 안 도는가 — 상수만 보고 믿지 않는다.
stag = [x for x in reasons if x and ("정체 정리" in x or "시간정리" in x)]
check("정체·시간정리 매도 0건 (STAGNANT_EXIT_ENABLED=False)", not stag,
      f"{len(stag)}건")
prio = [x for x in reasons if x and ("우선순위" in x or "슬롯 교체" in x)]
check("우선순위 교체 매도 0건 (일일한도 0)", not prio, f"{len(prio)}건")

# ══════════════════════════════════════════════════════════
section("[5] 🔴 변동성 손절 A/B — 규칙이 '실제로 달라지는가'")
# ══════════════════════════════════════════════════════════
# 08-10에 compute_stop_rate가 time.time() 기준으로 창을 잘라 리플레이에서
# 조용히 고정값으로 퇴화했다. ON/OFF 결과가 **완전히 동일**해서 발견했다.
# 스위트가 전원 통과해도 규칙이 무효일 수 있다 -> 매번 A/B로 확인한다.
probe_day = max(days, key=lambda d: sum(len(v) for v in DATA[d].values()))
r_on = replay_day(probe_day, DATA[probe_day], vol_stop=True)
r_off = replay_day(probe_day, DATA[probe_day], vol_stop=False)

on_rates = sorted({round(v, 4) for v in r_on.stop_rates.values() if v is not None})
off_rates = sorted({round(v, 4) for v in r_off.stop_rates.values() if v is not None})
buys_on = [b.get("stop_rate") for b in r_on.buys if b.get("stop_rate") is not None]
buys_off = [b.get("stop_rate") for b in r_off.buys if b.get("stop_rate") is not None]

print(f"       기준일 {probe_day} — ON 손절선 {on_rates}")
print(f"                    OFF 손절선 {off_rates}")
check("OFF는 전부 고정 STOP_LOSS_RATE",
      all(abs(v - SM.STOP_LOSS_RATE) < 1e-9 for v in buys_off) if buys_off else True,
      f"{sorted(set(buys_off))}")
check("🔴 ON/OFF가 실제로 다르다(규칙이 무효가 아니다)",
      bool(buys_on) and sorted(set(buys_on)) != sorted(set(buys_off)),
      f"ON {sorted(set(buys_on))[:4]}")
check("ON 손절선이 종목별로 차등된다", len(set(buys_on)) > 1 if buys_on else False,
      f"{len(set(buys_on))}종")
bad_clamp = [v for v in buys_on if not (SM.STOP_LOSS_VOL_MAX - 1e-9 <= v <= SM.STOP_LOSS_VOL_MIN + 1e-9)
             and abs(v - SM.STOP_LOSS_RATE) > 1e-9]
check(f"ON 손절선이 클램프 [{SM.STOP_LOSS_VOL_MAX}, {SM.STOP_LOSS_VOL_MIN}] 안",
      not bad_clamp, str(bad_clamp[:3]))

# ══════════════════════════════════════════════════════════
section("[6] 익절캡 상향(08-10)이 실제로 반영되는가")
# ══════════════════════════════════════════════════════════
caps = [x for x in reasons if x and "익절 캡" in x]
import re as _re
bad_cap = []
for x in caps:
    m = _re.search(r"순\+(\d+\.\d+)%", x)
    if m and float(m.group(1)) / 100.0 < SM.TAKE_PROFIT_CAP - 1e-6:
        bad_cap.append(x)
check(f"익절캡 청산은 전부 순 +{SM.TAKE_PROFIT_CAP*100:.1f}% 이상에서 난다",
      not bad_cap, f"{len(bad_cap)}건 {bad_cap[:1]}")
print(f"       익절캡 청산 {len(caps)}건 / 본전스톱 "
      f"{len([x for x in reasons if x and '본전스톱' in x])}건 / "
      f"손절 {len([x for x in reasons if x and '손절' in x])}건 / "
      f"VI {len([x for x in reasons if x and 'VI 상단' in x])}건")

# ══════════════════════════════════════════════════════════
section("[7] 🔴 스텁 충실도 — 감사가 거짓말할 수 있는 구조를 기계적으로 막는다")
# ══════════════════════════════════════════════════════════
# 08-10 실사고: `TradeRepository.update_sell`은 실물이 **trade_id를 위치 인자**로
# 받는데 스텁이 `(cls, **kw)`라 TypeError가 났고, 호출부의 except가 그걸 삼켜
# 'DB 종료 0건'이라는 **가짜 통과**가 나왔다. 그날 audit_deep과 리플레이만
# 고쳤는데, 나머지 11개 스위트에는 옛 스텁이 그대로 남아 있었다(08-10 이관 검증에서 발견).
# -> 스텁이 **실물이 실제로 받는 호출 형태 전부**를 받는지 기계적으로 센다.
import inspect as _insp
import re as _re2
from db.repository import TradeRepository as _RealTR

_real_sig = _insp.signature(_RealTR.update_sell)
# 실제 호출부 3곳의 형태 (positional trade_id 2곳 / keyword 1곳)
_PATTERNS = [
    ((1,), dict(sell_price=100, sell_quantity=10, exit_reason="x")),   # 유령정리·수동매도
    ((), dict(trade_id=1, sell_price=100, sell_quantity=10, exit_reason="x")),  # _execute_sell
]
for _a, _k in _PATTERNS:
    try:
        _real_sig.bind(*_a, **_k)
        _ok = True
    except TypeError:
        _ok = False
    check(f"실물 update_sell이 호출 형태를 받는다 ({'위치' if _a else '키워드'})", _ok)

_bad_stub = []
for _f in sorted(set(glob.glob(os.path.join(BASE_DIR, "test_*.py")))
                 | set(glob.glob(os.path.join(BASE_DIR, "audit_*.py")))):
    _src = open(_f, encoding="utf-8").read()
    _m = _re2.search(r"def update_sell\(([^)]*)\)", _src)
    if not _m:
        continue
    # 위치 인자를 하나라도 받을 수 있어야 한다 (cls 다음이 **kw면 못 받는다)
    if not _re2.match(r"\s*cls\s*,\s*[A-Za-z_]", _m.group(1)):
        _bad_stub.append(os.path.basename(_f))
check("모든 스위트의 update_sell 스텁이 위치 인자를 받는다",
      not _bad_stub, f"{len(_bad_stub)}개 {_bad_stub}")

print("\n" + "=" * 70)
print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건   ({_t.time() - T0:.1f}초)")
if FAIL:
    print("\n실패 목록:")
    for f in FAIL:
        print("  -", f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
