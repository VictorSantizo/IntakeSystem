@echo off
REM ============================================================
REM   Klickity Klack - install as a resilient background service
REM   RUN THIS ONCE on LACKSSERVER2021, AS ADMINISTRATOR.
REM   (Right-click -> Run as administrator)
REM ============================================================
REM
REM Registers a Windows Scheduled Task (via install_service.ps1) that starts
REM Klickity Klack at every boot under the SYSTEM account, ~1 minute after
REM startup, with restart-on-failure and no battery conditions. Result:
REM   - No console window for anyone to accidentally close
REM   - Keeps running after you log off / close Remote Desktop
REM   - Comes back on its own after a reboot / POWER OUTAGE
REM   - The in-app "Restart App" button still works (exit 42 -> relaunch)
REM
REM Output/errors are logged to klickity_klack.log in this folder.
REM
REM ALSO recommended (one time, admin cmd) so Windows boots straight through an
REM unclean shutdown instead of waiting at a recovery screen:
REM     bcdedit /set {current} bootstatuspolicy ignoreallfailures
REM     bcdedit /set {current} recoveryenabled No

set TASKNAME=KlickityKlack

REM Must be elevated to register a SYSTEM task.
net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo This must be run as Administrator.
  echo Right-click install_service.bat -> "Run as administrator".
  echo.
  pause
  exit /b 1
)

echo Registering the "%TASKNAME%" scheduled task ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_service.ps1"
if errorlevel 1 (
  echo.
  echo FAILED to register the task. See the message above.
  pause
  exit /b 1
)

echo.
echo Task registered. It will auto-start ~1 minute after every boot.
echo.

REM Start the service now -- but ONLY if nothing is already on port 5050, so we
REM don't end up with two copies fighting over it. If start.bat is still open,
REM close it first and re-run this, or run the schtasks /Run line shown below.
netstat -ano | findstr ":5050" | findstr /I "LISTENING" >nul 2>&1
if errorlevel 1 (
  echo Port 5050 is free -- starting the service now ...
  schtasks /Run /TN "%TASKNAME%"
  echo.
  echo Started in the background as SYSTEM ^(no window^). Give it a few seconds,
  echo then open http://localhost:5050 on the server to confirm.
) else (
  echo NOTE: Something is ALREADY listening on port 5050 -- almost certainly the
  echo       old start.bat window. CLOSE that window, then start the service with:
  echo.
  echo           schtasks /Run /TN "%TASKNAME%"
  echo.
)
echo.
pause
