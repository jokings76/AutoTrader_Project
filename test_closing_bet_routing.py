# -*- coding: utf-8 -*-
"""종가베팅 전용 검색식이 매매 라우팅을 오염시키지 않는지 검증.

가장 중요한 단언: 종가베팅 검색식에서 온 편입 신호는
strategy_mgr.on_condition_hit이 **한 번도 불리지 않아야** 한다.
(그냥 CONDITION_NAMES에 넣으면 resolve_strategy의 "둘 다 아님 -> 1A"
 폴백에 걸려 장중 1A 매수 후보가 되어버린다.)
"""
import sys, os, asyncio
sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8")

import main as M
import core.strategy_manager as SM

P = F = 0
def check(name, cond, detail=""):
    global P, F
    if cond:
        P += 1; print("  OK   | %s%s" % (name, (" -- " + str(detail)) if detail else ""))
    else:
        F += 1; print("  FAIL | %s -- %s" % (name, detail))


class _WS:
    def __init__(self, cmap): self.condition_map = cmap


def build_bot(cb_seq="9"):
    bot = M.TradingBot.__new__(M.TradingBot)      # __init__의 무거운 초기화 회피
    M.TradingBot.__init__(bot)
    bot.ws = _WS({"1": "주도주상위", "2": "돌파자동매매용",
                  "3": "눌림목자동", "9": "종가베팅"})
    bot._closing_bet_seq = cb_seq
    bot.calls = []
    class _Strat:
        _cond_names = {}
        _stock_names = {}
        def on_condition_hit(self, code, name, is_surge=False, cond_name=""):
            bot.calls.append((code, cond_name))
    bot.strategy_mgr = _Strat()
    bot.order_mgr = type("O", (), {"get_stock_name": staticmethod(lambda c: "N" + c)})()
    return bot


print("=" * 62)
print("[1] 종가베팅 편입이 매매 라우팅에 안 들어가는가")
print("=" * 62)
bot = build_bot()
asyncio.run(bot._on_signal("111111", "I", {"jmcode": "111111"}, cond_seq="9"))
check("on_condition_hit이 호출되지 않음", bot.calls == [], str(bot.calls))
check("종가베팅 유니버스에 편입됨", "111111" in bot._closing_bet_codes)
check("_known_hits 오염 없음(폴백 복원이 '종가베팅'을 만들지 않음)",
      "9" not in bot._known_hits, str(bot._known_hits))
check("매수시도 통계도 안 올라감", bot._signal_stats["buy_attempted"] == 0,
      str(bot._signal_stats))

print("\n" + "=" * 62)
print("[2] 이탈하면 유니버스에서 빠지는가 (14:50 멤버십 정확도)")
print("=" * 62)
asyncio.run(bot._on_signal("111111", "D", {"jmcode": "111111"}, cond_seq="9"))
check("이탈 시 유니버스에서 제거", "111111" not in bot._closing_bet_codes)

print("\n" + "=" * 62)
print("[3] 장중 검색식은 기존대로 매매 라우팅을 탄다 (회귀 방지)")
print("=" * 62)
bot = build_bot()
asyncio.run(bot._on_signal("222222", "I", {"jmcode": "222222"}, cond_seq="1"))
check("주도주상위는 on_condition_hit 호출됨", len(bot.calls) == 1, str(bot.calls))
check("cond_name이 '주도주상위'로 전달", bot.calls and bot.calls[0][1] == "주도주상위",
      str(bot.calls))
check("종가베팅 유니버스엔 안 들어감", "222222" not in bot._closing_bet_codes)

asyncio.run(bot._on_signal("333333", "I", {"jmcode": "333333"}, cond_seq="3"))
check("눌림목자동도 정상 라우팅", len(bot.calls) == 2 and bot.calls[1][1] == "눌림목자동",
      str(bot.calls))

print("\n" + "=" * 62)
print("[4] 같은 종목이 양쪽 검색식에 걸리면 둘 다 정상 동작")
print("=" * 62)
bot = build_bot()
asyncio.run(bot._on_signal("444444", "I", {"jmcode": "444444"}, cond_seq="1"))  # 주도주상위
asyncio.run(bot._on_signal("444444", "I", {"jmcode": "444444"}, cond_seq="9"))  # 종가베팅
check("장중 매매 라우팅은 탔다", any(c[0] == "444444" for c in bot.calls), str(bot.calls))
check("종가베팅 유니버스에도 들어갔다", "444444" in bot._closing_bet_codes)

print("\n" + "=" * 62)
print("[5] 검색식 미등록(seq 없음)이면 기존 동작 그대로 (안전 폴백)")
print("=" * 62)
bot = build_bot(cb_seq="")     # HTS에 '종가베팅' 조건식이 없는 상태
asyncio.run(bot._on_signal("555555", "I", {"jmcode": "555555"}, cond_seq="1"))
check("장중 신호는 정상 처리", len(bot.calls) == 1, str(bot.calls))
check("종가베팅 유니버스는 빈 채 유지(스캐너가 폴백함)",
      bot._closing_bet_codes == set())

print("\n" + "=" * 62)
print("[6] resolve_strategy — 종가베팅이 1A 폴백으로 새지 않는지(설계 근거)")
print("=" * 62)
from datetime import time as _t
# 이 단언은 '왜 분리했는지'를 코드로 남기는 것: 만약 cond_name에
# '종가베팅'이 실려 들어오면 실제로 1A가 된다 -> 그래서 애초에 안 넣는다.
r = SM.StrategyManager.resolve_strategy("종가베팅", _t(10, 0))
check("cond_name='종가베팅'이면 1A로 폴백된다(=넣으면 안 되는 이유)", r == "1A", r)
check("그래서 CONDITION_NAMES에 없어야 한다",
      "종가베팅" not in __import__("config.settings", fromlist=["x"]).CONDITION_NAMES)

print("\n" + "=" * 62)
print("[7] 낡은 STOP_SIGNAL이 기동을 죽이지 않는지 (2026-08-02 실사고)")
print("=" * 62)
# 2026-08-02: 13:59 정상종료 후 14:14에 만들어진 고아 STOP_SIGNAL이 남아
# 있었다. 그대로 뒀으면 다음날 08:59 무인 기동이 5초 만에 종료됐을 것이다.
import inspect
_setup_src = inspect.getsource(M.TradingBot.setup)
check("setup()이 기동 시 낡은 STOP_SIGNAL을 정리한다",
      "STOP_SIGNAL" in _setup_src and "remove" in _setup_src)
check("정리가 토큰 발급(=본격 기동)보다 먼저 일어난다",
      _setup_src.index("STOP_SIGNAL") < _setup_src.index("get_access_token"))
_gi = open(".gitignore", encoding="utf-8").read()
check("STOP_SIGNAL이 .gitignore에 등재됨(커밋되면 매번 되살아남)",
      "STOP_SIGNAL" in _gi)

print("\n" + "=" * 62)
print("통과 %d건 / 실패 %d건" % (P, F))
print("=" * 62)
sys.exit(1 if F else 0)
