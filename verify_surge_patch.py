#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
surge 라우팅 패치 검증기
─────────────────────────
사용법: 프로젝트 루트(AutoTrader_Project)에 이 파일을 두고
        python verify_surge_patch.py
13개 패치 항목이 들어갔는지 + .py 파일 문법(붙여넣기 깨짐)을 검사한다.
실제 매매는 일절 하지 않고 파일 텍스트만 읽는다.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def find(name: str, prefer_subdir: str = None):
    cands = []
    if prefer_subdir:
        cands.append(ROOT / prefer_subdir / name)
    cands.append(ROOT / name)
    for c in cands:
        if c.is_file():
            return c
    for p in ROOT.rglob(name):           # 최후수단: 재귀 검색 (.history 제외)
        if ".history" not in p.parts:
            return p
    return None


def read(p):
    if not p:
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


results = []  # (파일, 항목, 통과여부, 비고)


def check(file, label, ok, detail=""):
    results.append((file, label, bool(ok), detail))


# ── 파일 위치 ──
f_ini = find("config.ini")
f_set = find("settings.py", "config")
f_ws = find("kiwoom_ws.py", "api")
f_main = find("main.py")
f_sm = find("strategy_manager.py", "core")

# ── config.ini ──
t = read(f_ini)
check("config.ini", "파일 존재", f_ini is not None, str(f_ini) if f_ini else "못 찾음")
m = re.search(r"(?m)^\s*SURGE_CONDITION_NAMES\s*=\s*(.+)$", t)
check("config.ini", "① SURGE_CONDITION_NAMES 정의+값",
      bool(m and m.group(1).strip()),
      (m.group(1).strip() if m else "없음"))

# ── config/settings.py ──
t = read(f_set)
check("settings.py", "파일 존재", f_set is not None, str(f_set) if f_set else "못 찾음")
check("settings.py", "② SURGE_CONDITION_NAMES 파싱",
      ("SURGE_CONDITION_NAMES" in t) and ("_surge_names" in t))

# ── api/kiwoom_ws.py ──
t = read(f_ws)
check("kiwoom_ws.py", "파일 존재", f_ws is not None, str(f_ws) if f_ws else "못 찾음")
check("kiwoom_ws.py", "③-1 _cond_keys_logged", "_cond_keys_logged" in t)
check("kiwoom_ws.py", "③-2 cond_seq 추출", "cond_seq" in t)
check("kiwoom_ws.py", "③-2 raw 키 로그", "조건 실시간 raw 키" in t)
check("kiwoom_ws.py", "③-2 on_signal 4-인자 호출",
      bool(re.search(r"on_signal\(\s*stock_code\s*,\s*signal_type\s*,\s*item\s*,\s*cond_seq", t)))

# ── main.py ──
t = read(f_main)
check("main.py", "파일 존재", f_main is not None, str(f_main) if f_main else "못 찾음")
check("main.py", "④-1 self.surge_seqs", "self.surge_seqs" in t)
check("main.py", "④-2 setup 호출 _resolve_surge_seqs()", "self._resolve_surge_seqs()" in t)
check("main.py", "④-3 def _resolve_surge_seqs", "def _resolve_surge_seqs" in t)
check("main.py", "④-4 code_is_surge", "code_is_surge" in t)
n = len(re.findall(r"on_condition_hit\([^)]*is_surge\s*=", t))
check("main.py", "④-4/④-5 on_condition_hit(is_surge=) 2곳", n >= 2, f"{n}곳 발견")
check("main.py", "④-5 _on_signal cond_seq 인자",
      bool(re.search(r"def _on_signal\(self[^)]*cond_seq", t, re.S)))

# ── core/strategy_manager.py ──
t = read(f_sm)
check("strategy_manager.py", "파일 존재", f_sm is not None, str(f_sm) if f_sm else "못 찾음")
check("strategy_manager.py", "⑤-1 PHASE1_END=time(9,21)",
      bool(re.search(r"PHASE1_END\s*=\s*time\(\s*9\s*,\s*21\s*\)", t)))
check("strategy_manager.py", "⑤-1 (구)time(9,20) 제거됨",
      not re.search(r"PHASE1_END\s*=\s*time\(\s*9\s*,\s*20\s*\)", t))
check("strategy_manager.py", "⑤-2 SURGE_ENTRY_MIN", "SURGE_ENTRY_MIN" in t)
check("strategy_manager.py", "⑤-2 SURGE_MAX_SLOTS", "SURGE_MAX_SLOTS" in t)
check("strategy_manager.py", "⑤-3 def can_buy_surge", "def can_buy_surge" in t)
check("strategy_manager.py", "⑤-4 def evaluate_surge", "def evaluate_surge" in t)
check("strategy_manager.py", "⑤-5 on_condition_hit(is_surge)",
      bool(re.search(r"def on_condition_hit\(self[^)]*is_surge", t, re.S)))
check("strategy_manager.py", "⑤-5 surge 분기(evaluate_surge 호출)", "self.evaluate_surge(" in t)
check("strategy_manager.py", "⑤-6 _execute_buy 1S 라벨", 'sub_strategy == "1S"' in t)

# ── 출력 ──
GREEN, RED, DIM, RST = "\033[92m", "\033[91m", "\033[90m", "\033[0m"
try:
    import os
    os.system("")  # 윈도우 ANSI 활성화
except Exception:
    pass

print("\n" + "=" * 60)
print("  surge 라우팅 패치 검증 결과")
print("=" * 60)

cur = None
passed = failed = 0
for file, label, ok, detail in results:
    if file != cur:
        cur = file
        print(f"\n[{file}]")
    if ok:
        passed += 1
        mark = f"{GREEN}✓{RST}"
    else:
        failed += 1
        mark = f"{RED}✗{RST}"
    extra = f"  {DIM}({detail}){RST}" if detail and (not ok or "값" in label or "발견" in label) else ""
    print(f"  {mark} {label}{extra}")

# ── 문법 검사 ──
print(f"\n[파이썬 문법 검사 (붙여넣기 깨짐 탐지)]")
syntax_fail = 0
for label, p in [("api/kiwoom_ws.py", f_ws), ("main.py", f_main),
                 ("core/strategy_manager.py", f_sm), ("config/settings.py", f_set)]:
    if not p:
        print(f"  {RED}✗{RST} {label}: 파일 못 찾음")
        syntax_fail += 1
        continue
    try:
        ast.parse(read(p), filename=str(p))
        print(f"  {GREEN}✓{RST} {label}: 문법 OK")
    except SyntaxError as e:
        print(f"  {RED}✗{RST} {label}: 문법 오류 → {e.lineno}번 줄: {e.msg}")
        syntax_fail += 1

# ── 종합 ──
print("\n" + "=" * 60)
print(f"  항목: {passed}개 통과 / {failed}개 실패   |   문법오류: {syntax_fail}개")
if failed == 0 and syntax_fail == 0:
    print(f"  {GREEN}전부 통과! 개장 후 'python main.py'로 돌려봐.{RST}")
else:
    print(f"  {RED}위 ✗ 항목을 패치 가이드와 대조해서 마저 적용해.{RST}")
print("=" * 60 + "\n")

sys.exit(1 if (failed or syntax_fail) else 0)