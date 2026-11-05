@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe -m kuni run >> data\run_latest.log 2>&1
