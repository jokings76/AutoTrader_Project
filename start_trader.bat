@echo off
set PYTHON_EXE=C:\Users\rober\AppData\Local\Programs\Python\Python314\python.exe
cd /d "C:\AutoTrader_Bot\ProjectRoot"
if exist STOP_SIGNAL del STOP_SIGNAL

rem Claude Code remote-control session in a separate window (2026-08-02).
rem Launched FIRST so it is available even if the bot fails to start.
rem "start" returns immediately, so main.py below is not blocked.
rem NOTE: this file must stay pure ASCII - the real project path contains a
rem non-ASCII folder name and cmd.exe mangles it (see CLAUDE.md 2026-07-28).
rem Only the ASCII junction path appears here on purpose; all non-ASCII path
rem handling lives inside the .ps1, which PowerShell reads as UTF-8 safely.
start "AutoTrader RemoteControl" powershell -NoProfile -ExecutionPolicy Bypass -File "C:\AutoTrader_Bot\ProjectRoot\start_remote_control.ps1"

"%PYTHON_EXE%" notify_scheduler_start.py
"%PYTHON_EXE%" main.py
