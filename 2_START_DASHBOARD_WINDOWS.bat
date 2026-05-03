@echo off
cd /d "%~dp0"
title BTC.KILLER Dashboard

echo.
echo   Starting BTC.KILLER dashboard...
echo   Open http://localhost:5050 in your browser
echo.

python dashboard.py

pause
