"""
WebSocket 테스트 스크립트
- 토큰 발급 → WS 연결 → 조건식 목록 출력 → 실시간 등록 → 신호 대기
"""
import asyncio
from api.auth import get_access_token
from api.kiwoom_ws import KiwoomWS
from config import settings


async def on_signal(stock_code, signal_type, raw):
    kind = "편입" if signal_type == "I" else "이탈"
    print(f"📥 [{kind}] {stock_code}")


async def main():
    # 1) 토큰 발급
    token = get_access_token()
    if not token:
        print("❌ 토큰 발급 실패")
        return

    # 2) WS 연결 (on_signal 콜백 등록)
    ws = KiwoomWS(token, is_mock=True, on_signal=on_signal)
    await ws.connect()

    # 3) 조건식 목록 출력 (진짜 번호 확인용)
    cond_map = await ws.fetch_condition_list()
    print("\n" + "=" * 50)
    print("📋 키움 서버에 등록된 조건식 목록")
    print("=" * 50)
    for seq, name in cond_map.items():
        print(f"  seq={seq:>3}  →  {name}")
    print("=" * 50 + "\n")

  # 4) 조건식 실시간 등록 - 이름 기반 매칭
    target_names = settings.CONDITION_NAMES
    target_seqs = []

    if target_names:
        print(f"⚙️ 등록할 조건식: {target_names}\n")
        # 이름 → seq 역매칭
        name_to_seq = {name: seq for seq, name in cond_map.items()}
        for name in target_names:
            if name in name_to_seq:
                seq = name_to_seq[name]
                target_seqs.append(seq)
                print(f"   ✓ '{name}' → seq={seq}")
            else:
                print(f"   ⚠️ '{name}' 을(를) 찾지 못했습니다 - 영웅문 [0150] 이름 확인")
    else:
        # 폴백: 번호 기반
        print(f"⚙️ config.ini의 조건식 번호로 등록: {settings.CONDITION_NOS}\n")
        for seq in settings.CONDITION_NOS:
            seq = seq.strip()
            if seq in cond_map:
                target_seqs.append(seq)
            else:
                print(f"   ⚠️ seq={seq} 가 키움 서버에 없습니다")

    # 등록 실행
    for seq in target_seqs:
        await ws.subscribe_condition(seq)
   
    # 5) 무한 수신 루프
    await ws.listen()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 종료")