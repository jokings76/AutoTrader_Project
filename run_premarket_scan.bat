@echo off
rem Pre-market daily-pullback scanner (observation only, 2026-08-08).
rem Runs at 08:30, well before the bot starts at 08:59, so the two processes
rem never overlap - no token or REST contention.
rem
rem NOTE: this file must stay pure ASCII - the real project path contains a
rem non-ASCII folder name and cmd.exe mangles it (see CLAUDE.md 2026-07-28).
rem Only the ASCII junction path appears here on purpose.
set PYTHON_EXE=C:\Users\rober\AppData\Local\Programs\Python\Python314\python.exe
cd /d "C:\AutoTrader_Bot\ProjectRoot"
set PYTHONIOENCODING=utf-8
"%PYTHON_EXE%" premarket_scan.py
