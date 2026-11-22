@echo off
rem Readiverse launcher for Windows (run from source; double-click)
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
chcp 65001 >nul
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 app.py %*
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python not found. Install Python 3.10+ from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  python app.py %*
)
if errorlevel 1 pause
