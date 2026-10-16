@echo off
REM Klickity Klack - service-mode launcher (no pause, auto-restarts).
REM Used by the scheduled task created by install_service.bat. Loops forever:
REM relaunches on the in-app Restart (exit code 42) AND on an unexpected crash.
cd /d "%~dp0"
set PYTHON=%~dp0python\python.exe
set LOG=%~dp0klickity_klack.log
REM Python block-buffers stdout when it isn't attached to a terminal (i.e. once
REM redirected into the log below), so [AutoConfirm]/[Reclassify]/etc. prints
REM can lag well behind real time for anyone tailing the log live. Force
REM unbuffered so log lines land as they're printed.
set PYTHONUNBUFFERED=1

:loop
echo [%date% %time%] starting Klickity Klack >> "%LOG%"
"%PYTHON%" app.py >> "%LOG%" 2>&1
echo [%date% %time%] app exited with code %errorlevel%, restarting in 3s >> "%LOG%"
timeout /t 3 /nobreak >nul
goto loop
