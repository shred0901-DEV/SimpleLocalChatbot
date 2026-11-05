@echo off
setlocal
rem ============================================================
rem  SimpleLocalChatbot - setup script
rem
rem  - Creates .venv (only if missing)
rem  - Installs application dependencies (requirements.txt)
rem  - Idempotent: safe to run multiple times
rem
rem  Usage:
rem    setup.bat        application dependencies
rem    setup.bat --dev  also install dev/test dependencies
rem ============================================================
cd /d "%~dp0"

echo === SimpleLocalChatbot setup ===
echo.

set "PYCMD="
set "VENV_PY=.venv\Scripts\python.exe"
set "RC=0"

rem --- 1. Locate Python 3.11+ ----------------------------------
call :find_python
if not defined PYCMD (
    echo [ERROR] Python 3.11 or newer was not found.
    echo.
    echo Please install Python 3.11+ from https://www.python.org/downloads/
    echo and restart the terminal.
    set "RC=1"
    goto :finish
)
echo [OK] Using Python: %PYCMD%
%PYCMD% --version
echo.

rem --- 2. Create virtual environment (only if missing) ---------
if not exist "%VENV_PY%" (
    echo Creating virtual environment .venv ...
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv
        set "RC=1"
        goto :finish
    )
    echo [OK] .venv created
) else (
    echo [OK] .venv already exists, skipping creation
)
echo.

rem --- 3. Upgrade pip -------------------------------------------
echo Upgrading pip ...
"%VENV_PY%" -m pip install --upgrade pip >nul 2>nul
if errorlevel 1 echo [WARN] pip upgrade failed, continuing...

rem --- 4. Install application dependencies -----------------------
echo Installing application dependencies (requirements.txt) ...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install application dependencies.
    set "RC=1"
    goto :finish
)

rem --- 5. Install dev/test dependencies (optional) ---------------
if /i not "%~1"=="--dev" goto :skipdev
echo.
echo Installing dev dependencies (requirements-dev.txt) ...
"%VENV_PY%" -m pip install -r requirements-dev.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dev dependencies.
    set "RC=1"
    goto :finish
)
:skipdev

rem --- 6. Verify imports -----------------------------------------
echo.
"%VENV_PY%" -c "import PySide6, PIL; print('[OK] PySide6', PySide6.__version__, '/ Pillow', PIL.__version__)"
if errorlevel 1 (
    echo [ERROR] Package verification failed.
    set "RC=1"
    goto :finish
)

:finish
echo.
if "%RC%"=="0" (
    echo === Setup completed successfully. ===
    echo Launch the application with: run.bat
) else (
    echo === Setup FAILED. See the messages above. ===
)
echo.
pause
exit /b %RC%

:find_python
where py >nul 2>nul
if errorlevel 1 goto :find_python_path
py -3.11 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PYCMD=py -3.11"
    goto :eof
)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 (
    set "PYCMD=py -3"
    goto :eof
)

:find_python_path
where python >nul 2>nul
if errorlevel 1 goto :eof
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not errorlevel 1 set "PYCMD=python"
goto :eof

