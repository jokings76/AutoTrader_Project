"""
실시간 체결/호가 데이터 관찰 스크립트
─────────────────────────────────────
09:00~지정시간 동안 조건검색 편입 종목의 체결(0B) + 호가(0D) 데이터를
JSONL 파일로 저장. 이후 분석으로 전략 파라미터(체결강도/매도벽 임계값 등) 결정.

사용:
    python observe_realtime.py                  # 09:20 까지
    python observe_realtime.py --until 10:40    # 시간 지정
    python observe_realtime.py --duration 5     # 5분만 (테스트용)
"""
import argparse
import asyncio
import json
import os
import signal
import sys
from datetime import datetime, time, timedelta

from api.kiwoom_ws import KiwoomWS
from api.auth import get_access_token
from config import settings

OUTPUT_DIR = "observations"
os.makedirs(OUTPUT_DIR, exist_ok=True)


class Observer:
    def __init__(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.trade_file = open(f"{OUTPUT_DIR}/trades_{ts}.jsonl", "w", encoding="utf-8")
        self.orderbook_file = open(f"{OUTPUT_DIR}/orderbook_{ts}.jsonl", "w", encoding="utf-8")
        self.signal_file = open(f"{OUTPUT_DIR}/signals_{ts}.jsonl", "w", encoding="utf-8")

        # 통계 카운터
        self.subscribed: set[str] = set()
        self.trade_count = 0
        self.orderbook_count = 0
        self.signal_count = 0

        self.ws: KiwoomWS | None = None

    async def on_signal(self, stock_code, signal_type, raw):
        """편입 시 0B + 0D 구독 추가"""
        self.signal_count += 1
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "stock_code": stock_code,
            "signal_type": signal_type,
            "raw": raw,
        }
        self.signal_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.signal_file.flush()

        if signal_type == "I" and stock_code not in self.subscribed:
            await self.ws.subscribe_realtime([stock_code], ["0B", "0D"])
            self.subscribed.add(stock_code)
            print(f"  📡 {stock_code} 구독 추가 (누적 {len(self.subscribed)}종목)")

    async def on_trade(self, data):
        self.trade_count += 1
        record = {"ts": datetime.now().isoformat(timespec="milliseconds"), **data}
        self.trade_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    async def on_orderbook(self, data):
        self.orderbook_count += 1
        record = {"ts": datetime.now().isoformat(timespec="milliseconds"), **data}
        self.orderbook_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def flush(self):
        for f in (self.trade_file, self.orderbook_file, self.signal_file):
            f.flush()

    def close(self):
        for f in (self.trade_file, self.orderbook_file, self.signal_file):
            f.close()

    def stats(self) -> str:
        return (
            f"종목 {len(self.subscribed)}개 | "
            f"신호 {self.signal_count} | "
            f"체결 {self.trade_count} | "
            f"호가 {self.orderbook_count}"
        )


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--until", type=str, default="09:20",
                        help="종료 시각 HH:MM (기본 09:20)")
    parser.add_argument("--duration", type=int, default=None,
                        help="지속 시간(분). 지정 시 --until 무시. 테스트용.")
    args = parser.parse_args()

    # 종료 시각 결정
    if args.duration:
        end_at = datetime.now() + timedelta(minutes=args.duration)
        print(f"⏱  {args.duration}분 동안 관찰 (종료 ~ {end_at.strftime('%H:%M:%S')})")
    else:
        hh, mm = map(int, args.until.split(":"))
        end_time = time(hh, mm)
        now = datetime.now()
        end_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if end_time <= now.time():
            print(f"⚠️ 종료 시각({args.until})이 이미 지남. 다음날로 잡힐 수 있음.")
        print(f"⏱  {args.until} 까지 관찰")

    # 토큰 발급
    token = get_access_token()
    if not token:
        print("❌ 토큰 발급 실패. config.ini의 APP_KEY/SECRET_KEY 확인 필요.")
        return

    obs = Observer()
    ws = KiwoomWS(
        token=token,
        is_mock=settings.IS_MOCK,
        on_signal=obs.on_signal,
        on_trade=obs.on_trade,
        on_orderbook=obs.on_orderbook,
    )
    obs.ws = ws

    # SIGINT 처리
    stop_event = asyncio.Event()
    def handle_sigint():
        print("\n사용자 중단 요청")
        stop_event.set()
    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, handle_sigint)
    except NotImplementedError:
        pass  # Windows에서는 KeyboardInterrupt로 잡힘

    listen_task = None
    try:
        # 1. 연결 + 조건식 등록
        await ws.connect()
        await ws.fetch_condition_list()

        targets = settings.CONDITION_NAMES or []
        if not targets:
            print("⚠️ config.ini의 [SYSTEM] CONDITION_NAMES 비어있음. 종료.")
            return

        subscribed_seqs = []
        for name in targets:
            seq = next((s for s, n in ws.condition_map.items() if n == name), None)
            if seq:
                await ws.subscribe_condition(seq)
                subscribed_seqs.append((seq, name))
            else:
                print(f"⚠️ 조건식 '{name}'을 찾을 수 없음")
        if not subscribed_seqs:
            print("등록된 조건식 없음. 종료.")
            return

        print(f"\n관찰 시작. {len(subscribed_seqs)}개 조건식 등록.\n")

        # 2. 수신 루프 (백그라운드)
        listen_task = asyncio.create_task(ws.listen())

        # 3. 종료 조건 대기 (시간 도달 or Ctrl+C)
        while not stop_event.is_set():
            if datetime.now() >= end_at:
                print(f"\n⏰ 종료 시각 도달")
                break
            # 매 30초마다 진행 상황 출력
            await asyncio.sleep(30)
            obs.flush()
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] {obs.stats()}")

    except KeyboardInterrupt:
        print("\n사용자 중단 (Ctrl+C)")
    except Exception as e:
        print(f"\n에러: {e}")
        import traceback; traceback.print_exc()
    finally:
        if listen_task and not listen_task.done():
            listen_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass
        obs.close()
        print(f"\n📊 최종: {obs.stats()}")
        print(f"📁 저장 위치: {OUTPUT_DIR}/")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass