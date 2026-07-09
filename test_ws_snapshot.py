"""KiwoomWS 스냅샷 + 빈도 제한 + main 통합 단위 테스트"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unittest.mock import MagicMock, AsyncMock, patch
from api.kiwoom_ws import KiwoomWS, REG_INTERVAL_SEC


def _run(coro):
    return asyncio.run(coro)


def _make_ws():
    """테스트용 KiwoomWS 인스턴스 (실제 연결 없음)"""
    ws = KiwoomWS(token="test_token", is_mock=True)
    ws.ws = AsyncMock()
    ws.connected = True
    return ws


# ─────────────────────────────────────────────
# fetch_condition_snapshot
# ─────────────────────────────────────────────
def t1_snapshot_normal_response():
    print("\n[1] 조건식 스냅샷 - 정상 응답 (3종목)")

    async def run():
        ws = _make_ws()
        ws.condition_map = {"6": "장중급등단타"}
        fake_response = {
            "trnm": "CNSRREQ",
            "data": [
                {"9001": "A005930"},
                {"9001": "A035720"},
                {"9001": "A000660"},
            ],
        }
        ws._wait_for = AsyncMock(return_value=fake_response)
        ws._send = AsyncMock()
        return await ws.fetch_condition_snapshot("6")

    codes = _run(run())
    assert codes == ["005930", "035720", "000660"], f"got {codes}"
    print(f"  ✅ 3종목 추출 + 'A' 접두사 제거: {codes}")


def t2_snapshot_empty_response():
    print("\n[2] 조건식 스냅샷 - 빈 응답")

    async def run():
        ws = _make_ws()
        ws._wait_for = AsyncMock(return_value={"trnm": "CNSRREQ", "data": []})
        ws._send = AsyncMock()
        return await ws.fetch_condition_snapshot("6")

    codes = _run(run())
    assert codes == [], f"got {codes}"
    print(f"  ✅ 빈 리스트")


def t3_snapshot_timeout():
    print("\n[3] 조건식 스냅샷 - 타임아웃 시 빈 리스트 (예외 흡수)")

    async def run():
        ws = _make_ws()
        ws._wait_for = AsyncMock(side_effect=RuntimeError("타임아웃"))
        ws._send = AsyncMock()
        return await ws.fetch_condition_snapshot("6", timeout=1)

    codes = _run(run())
    assert codes == [], f"got {codes}"
    print(f"  ✅ 예외 처리됨, 빈 리스트 반환")


def t4_snapshot_various_keys():
    print("\n[4] 조건식 스냅샷 - 다양한 종목코드 키 처리")

    async def run():
        ws = _make_ws()
        fake_response = {
            "trnm": "CNSRREQ",
            "data": [
                {"9001": "A005930"},     # 표준 키
                {"jmcode": "A035720"},   # 모의투자 케이스
                {"stk_cd": "000660"},    # 'A' 없음
                {"foo": "bar"},          # 무시되어야 함
            ],
        }
        ws._wait_for = AsyncMock(return_value=fake_response)
        ws._send = AsyncMock()
        return await ws.fetch_condition_snapshot("6")

    codes = _run(run())
    assert codes == ["005930", "035720", "000660"], f"got {codes}"
    print(f"  ✅ 3가지 키 + 빈 항목 무시: {codes}")


def t5_snapshot_dict_response():
    print("\n[5] 조건식 스냅샷 - data가 dict 1개로 올 때")

    async def run():
        ws = _make_ws()
        fake_response = {
            "trnm": "CNSRREQ",
            "data": {"9001": "A005930"},
        }
        ws._wait_for = AsyncMock(return_value=fake_response)
        ws._send = AsyncMock()
        return await ws.fetch_condition_snapshot("6")

    codes = _run(run())
    assert codes == ["005930"], f"got {codes}"
    print(f"  ✅ dict 응답도 처리")


# ─────────────────────────────────────────────
# subscribe_realtime 빈도 제한
# ─────────────────────────────────────────────
def t6_reg_rate_limit():
    print(f"\n[6] subscribe_realtime - 연속 호출 시 {REG_INTERVAL_SEC*1000:.0f}ms 간격 보장")

    async def run():
        ws = _make_ws()

        timestamps = []
        async def track_send(payload):
            timestamps.append(asyncio.get_event_loop().time())
        ws._send = track_send

        await ws.subscribe_realtime(["005930"], ["0B", "0D"])
        await ws.subscribe_realtime(["035720"], ["0B", "0D"])
        await ws.subscribe_realtime(["000660"], ["0B", "0D"])

        return timestamps

    timestamps = _run(run())
    assert len(timestamps) == 3
    gap1 = timestamps[1] - timestamps[0]
    gap2 = timestamps[2] - timestamps[1]
    assert gap1 >= REG_INTERVAL_SEC - 0.05, f"gap1={gap1}"
    assert gap2 >= REG_INTERVAL_SEC - 0.05, f"gap2={gap2}"
    print(f"  ✅ 간격: {gap1*1000:.0f}ms, {gap2*1000:.0f}ms (≥{REG_INTERVAL_SEC*1000:.0f}ms)")


def t7_reg_first_call_immediate():
    print("\n[7] subscribe_realtime - 첫 호출은 즉시")

    async def run():
        ws = _make_ws()
        ws._send = AsyncMock()

        start = asyncio.get_event_loop().time()
        await ws.subscribe_realtime(["005930"], ["0B", "0D"])
        return asyncio.get_event_loop().time() - start

    elapsed = _run(run())
    assert elapsed < 0.1, f"첫 호출이 {elapsed*1000:.0f}ms 걸림"
    print(f"  ✅ 첫 호출 {elapsed*1000:.1f}ms (sleep 없음)")


# ─────────────────────────────────────────────
# main.py _process_initial_snapshot 통합
# ─────────────────────────────────────────────
def t8_initial_snapshot_integration():
    print("\n[8] _process_initial_snapshot - 통합 흐름 (중복 제거, 매수 평가, 구독)")

    async def run():
        from main import TradingBot

        bot = TradingBot()

        bot.ws = MagicMock()
        bot.ws.condition_map = {"6": "장중급등단타", "7": "프로불기둥단타"}
        # 조건식 6과 7에서 035720 중복
        bot.ws.fetch_condition_snapshot = AsyncMock(side_effect=[
            ["005930", "035720"],
            ["035720", "000660"],
        ])
        bot.ws.subscribe_realtime = AsyncMock()

        bot.strategy_mgr = MagicMock()

        bot.order_mgr = MagicMock()
        bot.order_mgr.get_stock_name = MagicMock(side_effect=lambda c: {
            "005930": "삼성전자",
            "035720": "카카오",
            "000660": "SK하이닉스",
        }.get(c, c))

        with patch("main.settings") as ms, \
             patch("main.SNAPSHOT_STAGGER_SEC", 0):  # 테스트 속도
            ms.CONDITION_NAMES = ["장중급등단타", "프로불기둥단타"]
            await bot._process_initial_snapshot()

        return bot

    bot = _run(run())

    # 스냅샷 카운트: 중복 제거되어 3
    assert bot._signal_stats["snapshot"] == 3, f"got {bot._signal_stats}"
    print(f"  ✅ snapshot 카운트: 3 (035720 중복 제거)")

    # on_condition_hit 3번
    calls = bot.strategy_mgr.on_condition_hit.call_args_list
    assert len(calls) == 3, f"got {len(calls)}"
    actual_codes = {c.args[0] for c in calls}
    assert actual_codes == {"005930", "035720", "000660"}
    print(f"  ✅ on_condition_hit 3번 호출: {sorted(actual_codes)}")

    # 종목명 정확히 전달
    code_name = {c.args[0]: c.args[1] for c in calls}
    assert code_name["005930"] == "삼성전자"
    assert code_name["035720"] == "카카오"
    assert code_name["000660"] == "SK하이닉스"
    print(f"  ✅ 종목명 REST 조회 fallback 동작")

    # subscribe_realtime 3번
    sub_calls = bot.ws.subscribe_realtime.call_args_list
    assert len(sub_calls) == 3
    print(f"  ✅ subscribe_realtime 3번 호출 (중복 없음)")


def t9_initial_snapshot_empty():
    print("\n[9] _process_initial_snapshot - 조건식 비어있을 때")

    async def run():
        from main import TradingBot

        bot = TradingBot()
        bot.ws = MagicMock()
        bot.ws.condition_map = {"6": "장중급등단타"}
        bot.ws.fetch_condition_snapshot = AsyncMock(return_value=[])
        bot.ws.subscribe_realtime = AsyncMock()
        bot.strategy_mgr = MagicMock()
        bot.order_mgr = MagicMock()

        with patch("main.settings") as ms:
            ms.CONDITION_NAMES = ["장중급등단타"]
            await bot._process_initial_snapshot()

        return bot

    bot = _run(run())
    assert bot._signal_stats["snapshot"] == 0
    assert not bot.strategy_mgr.on_condition_hit.called
    assert not bot.ws.subscribe_realtime.called
    print(f"  ✅ 0종목 → 호출 없음")


# ─────────────────────────────────────────────
# _extract_stock_name (모의투자 fallback)
# ─────────────────────────────────────────────
def t10_extract_stock_name():
    print("\n[10] _extract_stock_name - 키 누락 시 stock_code 반환 (fallback 트리거)")
    from main import _extract_stock_name

    # 모의투자: jmcode만 있음 → 종목명 못 찾음
    raw = {"jmcode": "A005930"}
    assert _extract_stock_name(raw, "005930") == "005930"
    print(f"  ✅ jmcode만 → 코드 반환 (fallback 필요 시그널)")

    # 정상 키
    raw = {"302": "삼성전자"}
    assert _extract_stock_name(raw, "005930") == "삼성전자"
    print(f"  ✅ '302' → 종목명")

    # raw None
    assert _extract_stock_name(None, "005930") == "005930"
    print(f"  ✅ None → 코드")

    # 빈 문자열도 코드 반환
    raw = {"302": "  "}
    assert _extract_stock_name(raw, "005930") == "005930"
    print(f"  ✅ 공백 문자열 → 코드 (whitespace 처리)")


if __name__ == "__main__":
    print("=" * 60)
    print("KiwoomWS 스냅샷 + 빈도 제한 + main 통합 검증")
    print("=" * 60)
    try:
        t1_snapshot_normal_response()
        t2_snapshot_empty_response()
        t3_snapshot_timeout()
        t4_snapshot_various_keys()
        t5_snapshot_dict_response()
        t6_reg_rate_limit()
        t7_reg_first_call_immediate()
        t8_initial_snapshot_integration()
        t9_initial_snapshot_empty()
        t10_extract_stock_name()
        print("\n" + "=" * 60)
        print("✅ 전체 통과")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n❌ 검증 실패: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 예외: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)