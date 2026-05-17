@echo off
rem Launch DLib as a desktop app (pywebview window, no subprocess spawning).
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "run_app.py"
