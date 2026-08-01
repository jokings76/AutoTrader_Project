# ══════════════════════════════════════════════════════════════════
# ⚠️ [삭제 예정 / DEPRECATED] — 2026-08-02 표시
#
# 이 파일은 **어디서도 import되지 않는 폐기된 코드**다.
# (main.py 기준 도달성 분석으로 확인 — 라이브 모듈 34개에 포함되지 않음)
#
# 지금 살아있는 전략은 **1A / Pullback 두 개뿐**이고, 둘 다 틱 구동
# (체결강도 FID228 3초 연속 + 대량체결 버스트)으로 동작한다.
# 아래 코드는 그 이전 설계(Phase2/Phase3/Surge/WallDetector FSM 등)의
# 잔재이므로 **현재 동작의 근거로 삼으면 안 된다.**
#
# 남겨둔 이유: CLAUDE.md 작업규칙 2("파일 삭제 금지 — _legacy로 보존").
# 삭제해도 git 히스토리로 언제든 복구 가능하므로, 다윤님이 판단해서
# 정리하면 된다. 정리 시 이 배너가 붙은 파일 전체가 대상이다.
#   확인:  git ls-files "*_legacy.py"
#   삭제:  git rm $(git ls-files "*_legacy.py")
# ══════════════════════════════════════════════════════════════════

import pandas as pd
from typing import Optional

# 유저님의 실제 모듈 경로 참조
from utils.logger import logger
from core.strategy.chemul_evaluator import ChemulState


def calc_ma(df: pd.DataFrame, window: int) -> pd.Series:
    """이동평균선 계산 유틸리티"""
    if df is None or "close" not in df.columns or len(df) < window:
        return pd.Series([0] * len(df) if df is not None else [])
    return df["close"].rolling(window=window).mean()


def score_surge(
    df: pd.DataFrame, theme_score: float = 0.0, current_idx: int = -1
) -> float:
    """급등주(Surge) 패턴 분석 및 점수 산정"""
    try:
        if df is None or len(df) < 20:
            return 0.0 + theme_score

        row = df.iloc[current_idx]
        prev_row = df.iloc[current_idx - 1]

        score = 0.0
        # 1. 가격 급등 배점
        change_rate = (row["close"] - prev_row["close"]) / prev_row["close"] * 100
        if change_rate >= 10.0:
            score += 40.0
        elif change_rate >= 5.0:
            score += 20.0

        # 2. 거래량 급증 배점
        vol_ma20 = df["volume"].rolling(20).mean().iloc[current_idx]
        vol_ratio = row["volume"] / (vol_ma20 + 1e-9)
        if vol_ratio >= 3.0:
            score += 40.0
        elif vol_ratio >= 2.0:
            score += 20.0

        # 3. 테마 점수 합산
        score += theme_score
        return float(min(score, 100.0))

    except Exception as e:
        logger.error(f"❌ score_surge 계산 중 에러 발생: {e}")
        return 0.0


def score_pullback(
    df: pd.DataFrame, chemul_score: float = 0.0, current_idx: int = -1
) -> float:
    """눌림목(Pullback) 점수 산정 (호가창 FSM 상태 반영)"""
    try:
        if df is None or len(df) < 20:
            return 0.0 + chemul_score

        row = df.iloc[current_idx]
        close = row["close"]
        ma20 = calc_ma(df, 20).iloc[current_idx]

        score = 0.0
        # 1. 20일선 이격도 배점 (눌림목 확인)
        diff_rate = abs(close - ma20) / ma20 * 100
        if diff_rate <= 2.0:
            score += 40.0
        elif diff_rate <= 4.0:
            score += 20.0

        # 2. 거래량 감소 배점 (조정 국면 확인)
        vol_ma5 = df["volume"].rolling(5).mean().iloc[current_idx]
        vol_ma20 = df["volume"].rolling(20).mean().iloc[current_idx]
        if vol_ma5 < vol_ma20:
            score += 20.0

        # 3. 체결강도/벽감지 점수(Phase1B) 합산
        score += chemul_score
        return float(min(score, 100.0))

    except Exception as e:
        logger.error(f"❌ score_pullback 계산 중 에러 발생: {e}")
        return 0.0


def calculate_total_score(
    stock_code: str,
    df: pd.DataFrame,
    mode: str = "surge",
    theme_manager=None,
    phase1b_controller=None,
    current_idx: int = -1,
) -> float:
    """
    [마스터 스코어링 함수]
    ThemeManager와 Phase1BController의 실제 데이터를 추출하여 최종 점수를 계산합니다.
    """
    theme_score = 0.0
    chemul_score = 0.0

    # 1. ThemeManager 연동 (주도 테마 판별)
    if theme_manager and hasattr(theme_manager, "code_to_theme"):
        theme_name = theme_manager.code_to_theme.get(stock_code)
        if theme_name:
            theme_score = 20.0
            logger.debug(
                f"🔥 [{stock_code}] 주도테마({theme_name}) 소속 확인: 가점 +20점"
            )

    # 2. Phase1BController 연동 (실시간 호가창/체결강도 FSM 상태 판별)
    if phase1b_controller and hasattr(phase1b_controller, "is_watching"):
        # 현재 감시 중인 종목인지 먼저 체크 (리소스 낭비 방지)
        if phase1b_controller.is_watching(stock_code):
            state: Optional[ChemulState] = phase1b_controller.get_state(stock_code)

            if state:
                state_name = getattr(state, "name", str(state)).upper()

                # FSM 상태에 따른 동적 가점 부여 (Enum 이름 기반)
                if state_name in ["BUY_TRIGGER", "STRONG_BUY", "TRIGGERED"]:
                    chemul_score = 40.0
                    logger.debug(
                        f"🎯 [{stock_code}] 체결강도 폭발({state_name}): 가점 +40점"
                    )
                elif state_name in ["PULLBACK_DETECTED", "WATCHING"]:
                    chemul_score = 20.0
                    logger.debug(
                        f"👀 [{stock_code}] 체결강도 감시({state_name}): 가점 +20점"
                    )

    # 3. 전략 모드별 메인 스코어링 호출
    if mode == "surge":
        return score_surge(df, theme_score=theme_score, current_idx=current_idx)
    elif mode == "pullback":
        return score_pullback(df, chemul_score=chemul_score, current_idx=current_idx)
    else:
        logger.warning(
            f"⚠️ 알 수 없는 모드({mode})입니다. 기본 surge 로직을 수행합니다."
        )
        return score_surge(df, theme_score=theme_score, current_idx=current_idx)
