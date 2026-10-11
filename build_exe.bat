@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   PhotoPilot - Windows EXE builder (one-click)
echo ============================================================
echo.
echo   This script turns the source code in this folder into a
echo   standalone program:  dist\PhotoPilot.exe
echo.
echo   Requirements: 64-bit Windows 10/11 and internet access.
echo ============================================================
echo.

REM ---------- 1) find Python ----------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python was not found on this computer.
    echo.
    echo   Please install 64-bit Python 3.11 first:
    echo     https://www.python.org/downloads/windows/
    echo   IMPORTANT: on the first install screen, tick
    echo     [x] Add python.exe to PATH
    echo.
    where winget >nul 2>nul && (
        echo   Or let winget try to install it now...
        winget install -e --id Python.Python.3.11
    )
    echo   After installing Python, run this script again.
    pause
    exit /b 1
)

echo [1/4] Python found:
%PY% --version
echo.

REM ---------- 2) create virtual environment ----------
if not exist ".venv-win\Scripts\python.exe" (
    echo [2/4] Creating virtual environment  .venv-win  ...
    %PY% -m venv .venv-win || goto :fail
) else (
    echo [2/4] Reusing existing virtual environment  .venv-win
)
echo.

set "VENV_PY=.venv-win\Scripts\python.exe"

REM ---------- 3) install dependencies ----------
echo [3/4] Installing dependencies (may take several minutes) ...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -e . || goto :fail
"%VENV_PY%" -m pip install pyinstaller || goto :fail
echo.

REM ---------- 4) build the exe ----------
echo [4/4] Building dist\PhotoPilot.exe ...
"%VENV_PY%" scripts\build_windows.py || goto :fail
echo.

echo ============================================================
echo   DONE
echo.
echo   Your program:  dist\PhotoPilot.exe
echo   Double-click it, or copy it to any Windows PC to run.
echo ============================================================
start "" "dist"
pause
exit /b 0

:fail
echo.
echo [ERROR] Build failed - please read the messages above.
echo   Common fixes:
echo     * make sure you are connected to the internet
echo     * close any program locking this folder
echo     * delete the .venv-win folder and run again
pause
exit /b 1
