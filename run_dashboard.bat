@echo off
REM ---------------------------------------------------------------
REM AutoTrader dashboard launcher (Phase 0, read-only)
REM
REM ASCII ONLY - do not put Korean text in this file, not even in
REM comments. The real project path contains a Korean folder name
REM which breaks cmd.exe parsing (see CLAUDE.md 2026-07-28).
REM That is why this file uses the ASCII junction path below.
REM ---------------------------------------------------------------
setlocal
set ROOT=C:\AutoTrader_Bot\ProjectRoot
if not exist "%ROOT%\ui\server.py" (
  echo [ERROR] Cannot find project at %ROOT%
  echo         Check the junction: dir C:\AutoTrader_Bot
  pause
  exit /b 1
)
cd /d "%ROOT%"
set PYTHONIOENCODING=utf-8
echo Starting dashboard on http://127.0.0.1:8787  (Ctrl+C to stop)
start "" http://127.0.0.1:8787
python -m ui.server
endlocal
