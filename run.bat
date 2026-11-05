@echo off
setlocal
rem ============================================================
rem  SimpleLocalChatbot - launcher
rem
rem  Runs main.py with the project .venv only.
rem ============================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv was not found.
    echo Please run setup.bat first to create the virtual environment.
    echo.
    pause
    exit /b 1
)

rem Keep diagnostic output available in a separate minimized console.
start "SimpleLocalChatbot - Console" /min "%~dp0.venv\Scripts\python.exe" "%~dp0main.py"
exit /b

