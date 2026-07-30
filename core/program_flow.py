"""프로그램 매매 유입 추적 — 분 단위 기록 + '꾸준히 들어오는 종목' 판정 (2026-07-31 신규).

목적
────
조건검색에 걸린 종목 중 "프로그램 매수가 분 단위로 꾸준히 들어오는 종목"을
기록해두고, 나중에 그 종목들의 실제 수익률이 어땠는지 백테스트할 수 있게 한다.
지금 당장 매매 판단에 쓰지 않는다 — **관측만 하고 기록만 남긴다**. 검증되지
않은 신호를 바로 매매에 넣지 않는 게 이 프로젝트의 기존 관례이고(OBV/체결강도
전부 실측 후 도입), 프로그램 순매수가 실제로 수익을 예측하는지는 아직 아무
근거가 없기 때문이다.

호출 예산에 대한 전제 (중요)
──────────────────────────
2026-07-30 로그 실측: 429(호출빈도 초과)가 하루 2,469건, 장중 매 분마다 발생.
api_id별로는 ka10080(분봉) 2,223건 / ka10027 231건 / ka20001 15건.
KiwoomREST.MIN_INTERVAL=0.6초라 이론상 분당 100콜이 상한인데, 그 상한에
계속 붙어 있다는 뜻이다. **즉 종목별 REST 폴링을 추가할 여유 예산이 없다.**
  - 종목 20개를 1분 주기로 폴링 = 분당 +20콜 (기존 대비 +20%) -> 불가
  - 종목 20개를 5분 주기 = 분당 +4콜 -> 여전히 부담
따라서 데이터 소스는 아래 우선순위로 잡는다:
  1순위) WebSocket 0B 실시간에 프로그램 FID가 실려 있으면 **추가 호출 0**.
         (확인용 진단 로그를 api/kiwoom_ws.py에 넣어둠 — 기동 후 "🔑 0B 체결
          raw 키" 한 줄로 판별)
  2순위) 시장 전체 랭킹형 REST 1건(예: 프로그램 순매수 상위 N종목)을 주기
         호출. 종목 수와 무관하게 **주기당 1콜**이라 1분 주기여도 분당 1콜.
         종목별 폴링(분당 20콜)의 1/20 비용으로 같은 목적을 달성한다.
  3순위) 종목별 폴링 — 429 문제를 먼저 해결하기 전에는 쓰지 말 것.

이 모듈은 소스를 가리지 않는다. record_minute()로 값만 넣어주면 되고,
누가 넣어주는지(WS 콜백/랭킹 폴러)는 호출부가 정한다.

'꾸준히'의 정의
──────────────
단순 누적 순매수는 한 분에 크게 들어온 것과 여러 분에 걸쳐 꾸준히 들어온 것을
구분하지 못한다. 이 프로젝트에서 필요한 건 후자이므로 세 축을 같이 기록한다:
  - positive_minutes : 최근 N분 중 순매수(+)였던 분의 수  -> '꾸준함'
  - max_streak       : 연속으로 순매수였던 최대 분 수      -> '끊김 없음'
  - cum_net          : 누적 순매수                          -> '규모'
백테스트에서 어느 축이 실제로 수익률을 예측하는지 비교할 수 있게 셋 다 남긴다
(어느 하나를 미리 정답으로 가정하지 않기 위함).
"""

from __future__ import annotations

import csv
import os
import threading
from datetime import datetime, timedelta

from utils.logger import logger

# 기록 보관 위치 — logs/ 아래에 날짜별 CSV. DB 스키마 변경 없이 바로 쌓이고,
# pandas로 그대로 읽어 백테스트할 수 있게 일부러 단순한 형식으로 둔다.
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "logs", "program_flow")

# 최근 몇 분을 보고 '꾸준함'을 판정할지
SUSTAIN_LOOKBACK_MIN = 10
# 그 구간에서 최소 몇 분이 순매수(+)여야 '꾸준히 들어온다'로 볼지
SUSTAIN_MIN_POSITIVE = 6
# 연속 유입 최소 분 수 (streak 기준 판정용)
SUSTAIN_MIN_STREAK = 3

# 분 단위 데이터를 종목당 몇 개까지 메모리에 들고 있을지 (장 6.5시간 = 390분)
MAX_MINUTES_KEPT = 400


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


class ProgramFlowTracker:
    """종목별 · 분별 프로그램 순매수를 누적하고, 꾸준한 유입 종목을 판정한다.

    스레드 안전: WS 콜백(별도 스레드)과 주기 태스크가 동시에 접근할 수 있어
    락으로 보호한다 — 이 프로젝트에서 _throttle/CandleCache가 같은 이유로
    락을 쓰다가 레이스를 겪은 전례가 있다(2026-07-28).
    """

    def __init__(self, now_func=None, out_dir: str = OUT_DIR):
        self._now = now_func or datetime.now
        self._out_dir = out_dir
        self._lock = threading.RLock()
        self._date = self._now().date()
        # code -> {minute(datetime): net_value(float)}
        self._minutes: dict[str, dict[datetime, float]] = {}
        # code -> 종목명 (로그 가독성용)
        self._names: dict[str, str] = {}
        self._flushed_until: dict[str, datetime] = {}  # code -> 마지막으로 CSV에 쓴 분

    # ------------------------------------------------------------------
    # 기록
    # ------------------------------------------------------------------
    def _reset_if_new_day(self):
        today = self._now().date()
        if today != self._date:
            self._date = today
            self._minutes.clear()
            self._flushed_until.clear()

    def record_minute(self, stock_code: str, net_value: float,
                      minute: datetime | None = None, stock_name: str = ""):
        """해당 분의 프로그램 순매수를 기록(같은 분에 여러 번 들어오면 덮어씀).

        net_value: 프로그램 순매수(양수=순매수, 음수=순매도). 단위는 호출부가
        정한 대로 일관되게만 넣으면 된다(원/천원/주 무관 — 백테스트는 부호와
        상대크기만 본다). 단위를 섞으면 종목 간 비교가 깨지므로 한 소스로 통일할 것.
        """
        if not stock_code:
            return
        with self._lock:
            self._reset_if_new_day()
            m = _floor_minute(minute or self._now())
            series = self._minutes.setdefault(stock_code, {})
            series[m] = float(net_value)
            if stock_name:
                self._names[stock_code] = stock_name
            if len(series) > MAX_MINUTES_KEPT:
                for old in sorted(series)[:len(series) - MAX_MINUTES_KEPT]:
                    series.pop(old, None)

    def add_minute_delta(self, stock_code: str, delta: float,
                         minute: datetime | None = None, stock_name: str = ""):
        """같은 분 안에서 여러 건이 들어오는 소스(WS 틱 등)를 위한 누적 버전."""
        if not stock_code:
            return
        with self._lock:
            self._reset_if_new_day()
            m = _floor_minute(minute or self._now())
            series = self._minutes.setdefault(stock_code, {})
            series[m] = series.get(m, 0.0) + float(delta)
            if stock_name:
                self._names[stock_code] = stock_name

    # ------------------------------------------------------------------
    # 판정
    # ------------------------------------------------------------------
    def metrics(self, stock_code: str, lookback_min: int = SUSTAIN_LOOKBACK_MIN,
                now: datetime | None = None) -> dict:
        """최근 lookback_min 분 구간의 유입 지표. 데이터 없으면 0으로 채운 dict.

        반환: {minutes, positive_minutes, negative_minutes, max_streak,
               cur_streak, cum_net, last_net}
        """
        with self._lock:
            series = dict(self._minutes.get(stock_code, {}))
        base = {"minutes": 0, "positive_minutes": 0, "negative_minutes": 0,
                "max_streak": 0, "cur_streak": 0, "cum_net": 0.0, "last_net": 0.0}
        if not series:
            return base

        end = _floor_minute(now or self._now())
        start = end - timedelta(minutes=lookback_min - 1)
        window = [(m, v) for m, v in sorted(series.items()) if start <= m <= end]
        if not window:
            return base

        pos = sum(1 for _, v in window if v > 0)
        neg = sum(1 for _, v in window if v < 0)
        cum = sum(v for _, v in window)

        max_streak = cur = 0
        for _, v in window:
            if v > 0:
                cur += 1
                max_streak = max(max_streak, cur)
            else:
                cur = 0

        return {
            "minutes": len(window),
            "positive_minutes": pos,
            "negative_minutes": neg,
            "max_streak": max_streak,
            "cur_streak": cur,
            "cum_net": cum,
            "last_net": window[-1][1],
        }

    def is_sustained(self, stock_code: str, now: datetime | None = None) -> bool:
        """'프로그램 매수가 꾸준히 들어오는 종목'인지.

        누적 순매수가 양수이면서(규모), 최근 구간의 과반이 순매수였고(꾸준함),
        연속 유입도 최소 기준을 넘어야(끊김 없음) True. 세 축을 AND로 묶은 건
        이 프로젝트에서 하락/이탈 판정을 OR로 걸었다가 과민해져 악화된 전례를
        따른 것(2026-07-30 동적 익절캡).
        """
        m = self.metrics(stock_code, now=now)
        if m["minutes"] < SUSTAIN_MIN_POSITIVE:
            return False  # 아직 구간이 안 찼으면 판단 보류
        return (
            m["cum_net"] > 0
            and m["positive_minutes"] >= SUSTAIN_MIN_POSITIVE
            and m["max_streak"] >= SUSTAIN_MIN_STREAK
        )

    def sustained_codes(self, now: datetime | None = None) -> list[tuple[str, dict]]:
        """꾸준한 유입 종목을 누적 순매수 큰 순으로 반환."""
        with self._lock:
            codes = list(self._minutes.keys())
        out = []
        for c in codes:
            if self.is_sustained(c, now=now):
                out.append((c, self.metrics(c, now=now)))
        out.sort(key=lambda x: x[1]["cum_net"], reverse=True)
        return out

    def tracked_count(self) -> int:
        with self._lock:
            return len(self._minutes)

    # ------------------------------------------------------------------
    # 영속화 (백테스트용)
    # ------------------------------------------------------------------
    def _csv_path(self) -> str:
        return os.path.join(self._out_dir, f"{self._date:%Y-%m-%d}.csv")

    def flush(self):
        """아직 파일에 안 쓴 '완성된 분'만 CSV에 append.

        현재 진행 중인 분은 값이 계속 변하므로 제외한다(분봉 캐시가 완성봉만
        신뢰하는 것과 같은 이유). 실패해도 매매에 영향 없도록 예외를 삼킨다."""
        try:
            with self._lock:
                self._reset_if_new_day()
                cur_minute = _floor_minute(self._now())
                rows = []
                for code, series in self._minutes.items():
                    last_done = self._flushed_until.get(code)
                    for m in sorted(series):
                        if m >= cur_minute:
                            continue  # 진행 중인 분은 아직 확정 아님
                        if last_done is not None and m <= last_done:
                            continue
                        met = self.metrics(code, now=m)
                        rows.append({
                            "date": f"{self._date:%Y-%m-%d}",
                            "minute": f"{m:%H:%M}",
                            "stock_code": code,
                            "stock_name": self._names.get(code, ""),
                            "net": round(series[m], 4),
                            "cum_net_10m": round(met["cum_net"], 4),
                            "positive_minutes_10m": met["positive_minutes"],
                            "max_streak_10m": met["max_streak"],
                            "sustained": int(self.is_sustained(code, now=m)),
                        })
                        self._flushed_until[code] = m
            if not rows:
                return 0
            os.makedirs(self._out_dir, exist_ok=True)
            path = self._csv_path()
            new_file = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                if new_file:
                    w.writeheader()
                w.writerows(rows)
            return len(rows)
        except Exception as e:
            logger.warning("프로그램 유입 CSV 기록 실패: %s", e)
            return 0

    # ------------------------------------------------------------------
    # 로깅
    # ------------------------------------------------------------------
    def report(self, top_n: int = 10, now: datetime | None = None) -> str:
        """꾸준한 유입 상위 종목 요약 한 줄 (정기 로그용)."""
        rows = self.sustained_codes(now=now)
        if not rows:
            return (f"프로그램 유입: 꾸준한 종목 없음 "
                    f"(추적 {self.tracked_count()}종목)")
        parts = []
        for code, m in rows[:top_n]:
            name = self._names.get(code, code)
            parts.append(
                f"{name}({code}) 누적{m['cum_net']:+,.0f} "
                f"{m['positive_minutes']}/{m['minutes']}분 연속{m['max_streak']}"
            )
        return (f"프로그램 유입 꾸준 {len(rows)}종목 (추적 {self.tracked_count()}) — "
                + " | ".join(parts))
