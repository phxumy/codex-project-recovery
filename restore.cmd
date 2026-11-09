@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -X utf8 "%~dp0codex_project_recovery.py" interactive
goto finished
:use_python
python -X utf8 "%~dp0codex_project_recovery.py" interactive
:finished
pause
