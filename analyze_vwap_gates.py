"""
VWAP 필터 게이트별 통과율/탈락률 분석 스크립트.

데이터 출처 두 곳을 합쳐야 전체 그림이 나옴:
  - 탈락 케이스: logs/autotrader.log* ("VWAP 필터 탈락" 라인, gates=dict 포함)
  - 통과 케이스: DB trades 테이블 (entry_reason에 "gates[...]" 포함)
    (통과 로그는 logger가 아니라 텔레그램/DB로만 남기 때문에 로그 파일만으로는
     통과율을 알 수 없음 — 반드시 DB와 합쳐야 함)

사용법: python analyze_vwap_gates.py
"""
import ast
import glob
import re
from collections import Counter

GATE_NAMES = ["adaptive", "slope", "reclaim", "band"]

REJECT_RE = re.compile(r"^\[(\d{6})\].*VWAP 필터 탈락.*gates=(\{.*\})\)\s*$")
PASS_GATES_RE = re.compile(r"gates\[([^\]]+)\]")
PASS_STOCK_RE = re.compile(r"^\[[^\]]*\]\s*Phase|^\[[^\]]*\].*")  # 참고용, 종목코드는 DB 컬럼에서 바로 가져옴


def parse_log_rejects() -> list[dict]:
    """logs/autotrader.log* 에서 VWAP 탈락 케이스 파싱."""
    rejects = []
    for path in sorted(glob.glob("logs/autotrader.log*")):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = REJECT_RE.search(line.strip())
                    if not m:
                        continue
                    stock_code, gates_repr = m.groups()
                    try:
                        gates = ast.literal_eval(gates_repr)
                    except (ValueError, SyntaxError):
                        continue
                    rejects.append({"stock_code": stock_code, "gates": gates})
        except FileNotFoundError:
            continue
    return rejects


def parse_db_passes() -> list[dict]:
    """DB trades 테이블에서 VWAP 통과(gates[...] 포함) 매수 기록 파싱."""
    try:
        from db.connection import get_cursor
    except Exception as e:
        print(f"(DB 모듈 로드 실패, 통과 케이스는 건너뜀: {e})")
        return []

    try:
        with get_cursor() as cur:
            cur.execute(
                "SELECT stock_code, entry_reason FROM trades "
                "WHERE entry_reason LIKE %s ORDER BY buy_time",
                ("%gates[%",),
            )
            rows = cur.fetchall()
    except Exception as e:
        print(f"(DB 조회 실패, 통과 케이스는 건너뜀: {e})")
        return []

    passes = []
    for row in rows:
        m = PASS_GATES_RE.search(row["entry_reason"] or "")
        if not m:
            continue
        gates = {}
        for token in m.group(1).split(","):
            token = token.strip()
            if not token:
                continue
            name, flag = token[:-1], token[-1]
            gates[name] = flag == "O"
        passes.append({"stock_code": row["stock_code"], "gates": gates})
    return passes


def main():
    rejects = parse_log_rejects()
    passes = parse_db_passes()
    total = len(rejects) + len(passes)

    print(f"통과(매수 체결): {len(passes)}건")
    print(f"탈락(VWAP 필터): {len(rejects)}건")
    print(f"전체 시도: {total}건")

    if total == 0:
        print("\n아직 분석할 데이터가 없습니다 (VWAP 5게이트 로직이 아직 실거래를 안 겪음).")
        return

    print(f"전체 통과율: {len(passes) / total * 100:.1f}%\n")

    print("게이트별 탈락 기여 횟수 (탈락 케이스 중 해당 게이트가 False였던 횟수):")
    gate_fail_count = Counter()
    for r in rejects:
        for gate in GATE_NAMES:
            if gate in r["gates"] and not r["gates"][gate]:
                gate_fail_count[gate] += 1
    for gate in GATE_NAMES:
        n = gate_fail_count[gate]
        pct = (n / len(rejects) * 100) if rejects else 0.0
        print(f"  {gate:10s}: {n:4d}건 (탈락 케이스의 {pct:.1f}%)")

    all_gates_ok_but_rejected = sum(
        1 for r in rejects if all(r["gates"].get(g, True) for g in GATE_NAMES)
    )
    print(
        f"\n4개 하드게이트는 다 통과했지만 confidence 미달로 탈락: "
        f"{all_gates_ok_but_rejected}건 "
        f"({all_gates_ok_but_rejected / len(rejects) * 100 if rejects else 0:.1f}%)"
    )


if __name__ == "__main__":
    main()
