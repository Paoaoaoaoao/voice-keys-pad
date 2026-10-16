@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem 有 venv 就用 venv，没有就用系统 python；都跟着这个 bat 所在目录走
if exist "%~dp0venv\Scripts\python.exe" (
  "%~dp0venv\Scripts\python.exe" -u "%~dp0ptt_pad.py" %*
) else (
  python -u "%~dp0ptt_pad.py" %*
)
if errorlevel 1 pause
