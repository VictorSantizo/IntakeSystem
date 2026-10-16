@echo off
title Klick Klack - Setup

echo ============================================
echo   Klick Klack - First-Time Setup
echo ============================================
echo.
echo NOTE: this prepares the bundled Python only. The app itself -
echo templates included - lives in git; nothing here generates code.
echo.

cd /d "%~dp0"
set PYTHON=%~dp0python\python.exe

if not exist "%PYTHON%" (
    echo ERROR: Local Python not found.
    echo Expected: %PYTHON%
    echo Please unzip the embeddable package to python\ beside this script.
    pause
    exit /b 1
)

echo Found Python at: %PYTHON%
echo.

echo [1/3] Enabling pip support in embeddable Python...
for %%f in ("%~dp0python\python*._pth") do (
    powershell -Command "(Get-Content '%%f') -replace '#import site', 'import site' | Set-Content '%%f'"
    echo   Fixed: %%~nxf
)
echo.

echo [2/3] Downloading pip...
powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%~dp0get-pip.py'"
"%PYTHON%" "%~dp0get-pip.py"
del "%~dp0get-pip.py"
echo.

echo [3/3] Installing required packages (requirements.txt)...
"%PYTHON%" -m pip install -r "%~dp0requirements.txt"
echo.

echo ============================================
echo   Setup complete! Run start.bat to launch.
echo ============================================
echo.
pause
