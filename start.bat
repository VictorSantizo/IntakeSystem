@echo off
title Klickity Klack

echo ============================================
echo   Klickity Klack  -  Property Management Intake
echo ============================================
echo.

cd /d "%~dp0"
set PYTHON=%~dp0python\python.exe

if not exist "%PYTHON%" (
    echo ERROR: Local Python not found.
    echo Please run setup.bat first.
    pause
    exit /b 1
)

set PYTHONPATH=%~dp0
echo Starting intake system...
echo Access from any computer at: http://192.168.10.240:5050
echo.
echo Press Ctrl+C to stop the server.
echo.

:loop
"%PYTHON%" app.py
if %errorlevel%==42 (
    echo Restarting...
    goto loop
)
pause
