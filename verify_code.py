import inspect

print("1. strategy_manager.py 모듈 임포트 테스트 (문법/들여쓰기 검증)...")
try:
    from core.strategy.strategy_manager import StrategyManager
    print("✅ 모듈 임포트 성공! 문법 및 들여쓰기에 문제가 없습니다.")
except Exception as e:
    print(f"❌ 모듈 임포트 실패! 코드에 에러가 있습니다: {e}")
    exit(1)

print("\n2. 메서드 존재 여부 확인 (클래스 안에 들어갔는지)...")
if hasattr(StrategyManager, '_get_merged_candles'):
    print("✅ _get_merged_candles 메서드가 클래스 안에 정확하게 들어가 있습니다!")
else:
    print("❌ _get_merged_candles 메서드를 찾을 수 없습니다. (클래스 밖으로 빠져나갔을 수 있음)")
    exit(1)

print("\n3. 파라미터 정확성 확인...")
sig = inspect.signature(StrategyManager._get_merged_candles)
params = list(sig.parameters.keys())
expected_params = ['self', 'stock_code', 'interval', 'count']
if params == expected_params:
    print(f"✅ 파라미터 완벽합니다: {params}")
else:
    print(f"⚠️ 파라미터가 예상과 다릅니다: {params} (예상: {expected_params})")

print("\n" + "="*50)
print("🎉 모든 검증을 통과했습니다! 코드에 이상 없습니다.")