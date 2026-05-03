@echo off
cd /d "%~dp0"
title BTC.KILLER Setup

echo ========================================
echo   BTC.KILLER - Setup
echo ========================================
echo.

:: Find Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not found.
    echo.
    echo Install from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo Found Python:
python --version
echo.

:: Install dependencies
echo Installing dependencies...
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to install dependencies.
    echo Try running as Administrator.
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Setup complete!
echo   Run 2_START_DASHBOARD_WINDOWS.bat to start.
echo ========================================
echo.
pause
