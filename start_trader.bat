@echo off
set PYTHON_EXE=C:\Users\rober\AppData\Local\Programs\Python\Python314\python.exe
cd /d "C:\Users\rober\OneDrive\문서\vscode\AutoTrader_Project"
if exist STOP_SIGNAL del STOP_SIGNAL
"%PYTHON_EXE%" notify_scheduler_start.py
"%PYTHON_EXE%" main.py
