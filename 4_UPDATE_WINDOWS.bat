@echo off
cd /d "%~dp0"
title BTC.KILLER Update

echo ========================================
echo   BTC.KILLER - Check for Updates
echo ========================================
echo.

:: Check if git repo
if not exist ".git" (
    echo ERROR: This folder wasn't installed via git clone.
    echo.
    echo To get future updates, reinstall using:
    echo   git clone YOUR_GITHUB_REPO_URL
    echo.
    echo Then copy your .env file and private key into the new folder.
    echo.
    pause
    exit /b 1
)

echo Fetching latest version from GitHub...
git fetch origin main
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Could not reach GitHub. Check your internet connection.
    pause
    exit /b 1
)

echo.
echo Applying latest files...
git checkout origin/main -- bot.py dashboard.py dashboard.html requirements.txt HOW_TO_USE.txt 1_SETUP_WINDOWS.bat 2_START_DASHBOARD_WINDOWS.bat 4_UPDATE_WINDOWS.bat .gitignore

if %errorlevel% equ 0 (
    echo.
    echo ========================================
    echo   Updated successfully!
    echo   Restart the dashboard to apply changes.
    echo ========================================
) else (
    echo.
    echo ERROR: Update failed. Try deleting this folder and cloning fresh.
)

echo.
pause
