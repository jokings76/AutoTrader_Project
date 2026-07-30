"""장중 전략 성과 추적 → 슬롯 우선순위 자동 조정 (2026-07-31 신규).

목적: "오늘 유난히 잘 되는 전략이 있으면 그쪽으로 슬롯을 더 태운다"를 사람의
판단 없이 장중에 자동으로 하게 만든다. 반대로 오늘 유난히 안 되는 전략은
스스로 물러나게 한다.

────────────────────────────────────────────────────────────
왜 '관측 평균 그대로'를 쓰면 안 되는가 (설계의 핵심)
────────────────────────────────────────────────────────────
실측(trades DB, 07-28~07-30 체제 개편 이후)에서 전략별 하루 표본은 3~27건에
불과했고, 1B는 하루 만에 건당 -3.26%(07-29) -> +0.73%(07-30)로 부호가 뒤집혔다.
장중에는 표본이 이보다 훨씬 작다(오전 10시 기준 전략당 2~5건). 이 상태에서
관측 평균을 그대로 믿고 슬롯을 몰아주면 '방금 운이 좋았던 전략'을 추종하게
되고, 실제로 07-29 성과로 07-30을 판단했다면 유일하게 플러스였던 전략을
막았을 것이다.

그래서 두 겹으로 방어한다:
  1) 축소추정(shrinkage) — 관측 평균을 '우위 없음(0)' 쪽으로 끌어당긴다.
         adj = (n * observed) / (n + SHRINK_K)
     표본이 적을수록 0에 눌려 자동으로 중립이 되고, 표본이 쌓일수록 관측값에
     가까워진다. 임의의 컷오프 없이 표본 크기가 신뢰도를 결정하게 하는 방식.
  2) 최소 표본 + 조정폭 상한 — MIN_SAMPLE 미만이면 아예 판단하지 않고,
     판단이 서더라도 컷라인 변화는 ±MAX_CUTLINE_ADJUST 이내로 묶는다.
     최악의 경우에도 시스템이 한쪽으로 쏠려 무너지지 않게 하는 안전장치.

────────────────────────────────────────────────────────────
왜 '슬롯 개수'가 아니라 '점수 컷라인'을 조정하는가
────────────────────────────────────────────────────────────
전략별 슬롯 상한(3)과 공유 상한(6/8)은 리스크 분산 장치다. 성과에 따라 이걸
직접 늘리면 잘 나가는 전략 하나에 자본이 집중돼 그 전략이 꺾이는 순간
손실도 같이 집중된다. 반면 컷라인 조정은 "더 좋은 후보만 통과시킨다"는
품질 축으로 작동해서, 우대해도 아무 종목이나 사지 않고 제한해도 정말 좋은
후보는 여전히 통과한다. 이 시스템의 진입 판정이 전부 '점수 대 컷라인'
비교이므로 기존 구조와도 자연스럽게 맞물린다.
  우대(HOT)  -> 컷라인 인하 = 진입 문턱이 낮아져 슬롯을 더 자주 가져감
  제한(COLD) -> 컷라인 인상 + 공유 슬롯 마지막 칸 양보

한계(반드시 인지할 것): 이 로직은 과거 틱/체결 데이터로 백테스트 검증되지
않았다. 장중 실시간 성과에 반응하는 구조라 daily_backtest도 그대로 재현하지
못한다(백테스트는 이 조정 없이 고정 컷라인으로 돈다). 파라미터는 전부
보수적으로 잡았고, ENABLED=False로 즉시 끌 수 있게 해뒀다.
"""

from datetime import datetime, time
from utils.logger import logger

# 전체 스위치 — 실전에서 이상 징후가 보이면 이것만 False로 두면 기존 동작(고정
# 컷라인)으로 완전히 돌아간다. 조정 로직이 꺼져도 성과 기록/로깅은 계속된다.
ENABLED = True

# 이 시각 이후부터 판정 시작. 개장 직후는 표본이 없기도 하고, 초반 몇 건의
# 결과로 하루 전체 기조가 정해지는 것을 막는 의미도 있다.
ACTIVE_FROM = time(9, 20)

# 최소 표본 — 이 미만이면 축소추정 이전에 아예 판단을 보류(중립).
MIN_SAMPLE = 3

# 축소 상수(pseudo-count). 클수록 보수적. 5면 표본 5건일 때 관측값의 절반만
# 반영한다(n/(n+5) = 0.5). 실측 일일 표본이 한 자릿수인 점을 감안한 값.
SHRINK_K = 5.0

# 컷라인 조정폭 상한 (±15%). 1A 컷라인 6.5 기준 5.5~7.5 범위.
MAX_CUTLINE_ADJUST = 0.15

# 조정폭이 최대가 되는 기준 기대손익(±1.0%). 축소추정된 기대손익이 이 값에
# 도달하면 조정폭 상한을 그대로 쓰고, 그 사이는 선형 보간한다.
NORMALIZE_RATE = 0.01

# 등급 경계 — HOT은 이익 쪽에서 좀 더 엄격하게(+0.4%), COLD는 손실 쪽에서
# 조금 느슨하게(-0.6%) 잡았다. 우대는 신중하게, 제한은 상대적으로 빨리
# 걸리게 해서 비대칭적으로 방어에 유리하도록 한 의도적 선택.
HOT_EDGE = 0.004
COLD_EDGE = -0.006

# COLD 전략이 양보할 공유 슬롯 수 — COLD면 MAX_HOLDINGS-1까지만 사용해서
# 마지막 한 칸을 다른 전략에 남긴다.
COLD_SHARED_RESERVE = 1

# HOT 전략의 확장 슬롯 점수 마진 완화 (기본 1.5 -> 1.3).
HOT_EXPANSION_MARGIN = 1.3

# 기록 시 극단값 클램프 (±10%). 손절 -3%/익절캡 2.5% 구조에서 이를 크게 벗어난
# 값은 데이터 이상일 가능성이 높고, 표본이 한 자릿수인 이 로직에서는 그런 값
# 하나가 등급을 통째로 뒤집는다. record() 주석 참고.
OUTLIER_CLAMP = 0.10


class StrategyPerformanceTracker:
    """전략별 당일 실현손익을 모아 축소추정 기대손익과 등급을 산출한다.

    now_func: StrategyManager와 같은 시계를 쓰기 위해 주입(테스트 용이성).
    """

    def __init__(self, now_func=None):
        self._now = now_func or datetime.now
        self._date = self._now().date()
        self._records: dict[str, list[float]] = {}  # sub_strategy -> [net_rate(소수), ...]
        self._last_tier: dict[str, str] = {}        # 등급 전이 로깅용

    # ---------------------------------------------------------------
    # 기록
    # ---------------------------------------------------------------
    def _reset_if_new_day(self):
        today = self._now().date()
        if today != self._date:
            self._date = today
            self._records.clear()
            self._last_tier.clear()

    def record(self, sub_strategy: str, net_rate: float):
        """청산 완료 시 호출. net_rate는 수수료 차감 순수익률(소수, 0.015 = +1.5%).

        극단값은 OUTLIER_CLAMP로 자른다. 손절 -3%/익절캡 2.5% 구조상 정상
        범위를 크게 벗어난 값은 데이터 이상일 가능성이 높은데(실제로 DB의
        profit_rate는 부분체결 버그로 -39%/-46% 같은 허위값이 기록된 전례가
        있다 — 2026-07-30 수정), 표본이 한 자릿수인 이 로직에서는 그런 값
        하나가 전략 등급을 통째로 뒤집는다. 여기서 쓰는 net_rate 자체는
        가격 기반이라 그 버그의 영향을 받지 않지만, 갭하락 등으로 손절선을
        크게 넘겨 체결되는 경우까지 감안해 방어선을 둔다."""
        if not sub_strategy:
            return
        self._reset_if_new_day()
        clamped = max(-OUTLIER_CLAMP, min(OUTLIER_CLAMP, float(net_rate)))
        if clamped != float(net_rate):
            logger.warning(
                "[전략성과] %s 극단 손익 %+.2f%% -> %+.2f%%로 제한 후 반영",
                sub_strategy, float(net_rate) * 100, clamped * 100,
            )
        self._records.setdefault(sub_strategy, []).append(clamped)

    # ---------------------------------------------------------------
    # 판정
    # ---------------------------------------------------------------
    def sample_count(self, sub_strategy: str) -> int:
        self._reset_if_new_day()
        return len(self._records.get(sub_strategy, ()))

    def observed_edge(self, sub_strategy: str) -> float | None:
        """축소 전 관측 평균 (로그/진단용). 표본 없으면 None."""
        rows = self._records.get(sub_strategy)
        if not rows:
            return None
        return sum(rows) / len(rows)

    def adjusted_edge(self, sub_strategy: str) -> float | None:
        """축소추정 기대손익. 판단 불가(비활성/시간 전/표본 부족)면 None.

        adj = (n * observed) / (n + SHRINK_K)
        관측 평균을 0(우위 없음) 쪽으로 끌어당긴 값 — 표본이 적을수록 강하게
        눌린다. 예: 관측 +2.0%가 n=3이면 +0.75%, n=10이면 +1.33%."""
        if not ENABLED:
            return None
        self._reset_if_new_day()
        if self._now().time() < ACTIVE_FROM:
            return None
        rows = self._records.get(sub_strategy)
        if not rows or len(rows) < MIN_SAMPLE:
            return None
        n = len(rows)
        observed = sum(rows) / n
        return (n * observed) / (n + SHRINK_K)

    def tier(self, sub_strategy: str) -> str:
        """HOT(우대) / NEUTRAL(기본) / COLD(제한)."""
        adj = self.adjusted_edge(sub_strategy)
        if adj is None:
            return "NEUTRAL"
        if adj >= HOT_EDGE:
            return "HOT"
        if adj <= COLD_EDGE:
            return "COLD"
        return "NEUTRAL"

    def cutline_multiplier(self, sub_strategy: str) -> float:
        """점수 컷라인에 곱할 배수. 1.0이면 조정 없음.

        축소추정 기대손익을 NORMALIZE_RATE로 정규화해 ±MAX_CUTLINE_ADJUST
        범위로 선형 매핑한다(클램프). 등급 경계에서 값이 튀지 않도록 등급이
        아니라 연속값을 그대로 쓰는 게 핵심 — 경계 근처에서 컷라인이
        왔다갔다하며 진입이 흔들리는 것을 막는다."""
        adj = self.adjusted_edge(sub_strategy)
        if adj is None:
            return 1.0
        ratio = max(-1.0, min(1.0, adj / NORMALIZE_RATE))
        return 1.0 - MAX_CUTLINE_ADJUST * ratio

    def shared_slot_limit(self, sub_strategy: str, base_limit: int) -> int:
        """COLD 전략이 쓸 수 있는 공유 슬롯 상한 (다른 전략에 마지막 칸 양보)."""
        if self.tier(sub_strategy) != "COLD":
            return base_limit
        return max(1, base_limit - COLD_SHARED_RESERVE)

    def expansion_margin(self, sub_strategy: str, base_margin: float) -> float:
        """확장 슬롯 점수 마진 — HOT 전략은 완화해서 확장 슬롯을 더 쉽게 쓴다."""
        if self.tier(sub_strategy) != "HOT":
            return base_margin
        return min(base_margin, HOT_EXPANSION_MARGIN)

    # ---------------------------------------------------------------
    # 로깅
    # ---------------------------------------------------------------
    def log_tier_change(self, sub_strategy: str):
        """등급이 바뀐 순간에만 로그를 남긴다(핫패스 스팸 방지)."""
        tier = self.tier(sub_strategy)
        if self._last_tier.get(sub_strategy) == tier:
            return
        self._last_tier[sub_strategy] = tier
        adj = self.adjusted_edge(sub_strategy)
        if adj is None:
            return
        logger.info(
            "[전략성과] %s 등급 -> %s (표본 %d건, 관측 %+.2f%% -> 축소 %+.2f%%, "
            "컷라인 x%.2f)",
            sub_strategy, tier, self.sample_count(sub_strategy),
            (self.observed_edge(sub_strategy) or 0) * 100, adj * 100,
            self.cutline_multiplier(sub_strategy),
        )

    def summary(self) -> str:
        """전략별 현황 한 줄 요약 (정기보고/텔레그램용)."""
        self._reset_if_new_day()
        if not self._records:
            return "전략성과: 청산 기록 없음"
        parts = []
        for sub in sorted(self._records):
            n = len(self._records[sub])
            obs = (self.observed_edge(sub) or 0) * 100
            adj = self.adjusted_edge(sub)
            tier = self.tier(sub)
            if adj is None:
                parts.append(f"{sub} {n}건 {obs:+.2f}%(판단보류)")
            else:
                parts.append(
                    f"{sub} {n}건 {obs:+.2f}%->{adj*100:+.2f}% [{tier} x{self.cutline_multiplier(sub):.2f}]"
                )
        return "전략성과: " + " | ".join(parts)
