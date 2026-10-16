@echo off
rem  Connect My Scanner - joins THIS computer to the Klick Klack scan flow.
rem  Starts the scan connector now and drops a tiny command in the Startup
rem  folder so it runs again at every sign-in. Safe to re-run any time.
rem
rem  Path policy: drive letters vary from desk to desk, so everything this
rem  installer PERSISTS uses the server's real UNC path. It runs fine from a
rem  downloaded copy (the Scan page offers it as a download), from the UNC
rem  share, or from any mapped letter.
title Connect My Scanner - Klick Klack
setlocal
set "UNC=\\LACKSSERVER2021\File-Cabinet\IntakeSystem"
set "SHARE=%UNC%"
if not exist "%SHARE%\scan_connector.py" (
  set "SHARE=%~dp0"
  if "%SHARE:~-1%"=="\" set "SHARE=%SHARE:~0,-1%"
)
if not exist "%SHARE%\scan_connector.py" set "SHARE=T:\IntakeSystem"
set "PYW=%SHARE%\python\pythonw.exe"
set "SCRIPT=%SHARE%\scan_connector.py"
if not exist "%PYW%" (
  echo Could not reach the Klick Klack server files at
  echo   %UNC%
  echo Is this computer on the office network?
  pause
  exit /b 1
)

rem  Startup entry: plain batch on purpose - no powershell, no script-made
rem  .lnk shortcuts (VIPRE dislikes those). Replaces the .lnk that older
rem  versions of this installer created. No working-directory needed: the
rem  connector resolves everything from its own file location.
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
echo @start "" "%PYW%" "%SCRIPT%">"%STARTUP%\Klick Klack Scan Connector.cmd"
del "%STARTUP%\Klick Klack Scan Connector.lnk" >nul 2>&1
start "" "%PYW%" "%SCRIPT%"

rem  If this machine lacks the Klick Klack profile but a shared exported copy
rem  exists (made once via the Scan window's "..." menu - Import/Export
rem  profiles - Export), open it so ScanSnap Home offers the 3-click Import.
set "SSXML=%APPDATA%\PFU\ScanSnap Home\Managed\Profiles\PfuSsHome.xml"
if exist "%SSXML%" (
  findstr /i /c:"Klick Klack" "%SSXML%" >nul 2>&1
  if errorlevel 1 (
    for %%F in ("%SHARE%\Klick Klack Profile.*") do (
      echo   Opening the shared Klick Klack scan profile - click Import when
      echo   ScanSnap Home asks, then this computer is fully set up.
      start "" "%%~fF"
      goto :profile_done
    )
    echo   One more one-time step: add the "Klick Klack" profile in ScanSnap
    echo   Home - the Scan page in Klick Klack shows you how, or see
    echo   SCANNER_SETUP.md Step 2.
  )
)
:profile_done
echo.
echo   Connected! This computer now reports its scanner to Klick Klack.
echo   Go back to the Scan page in Klick Klack - it flips green by itself
echo   within about half a minute.
echo.
echo   First time on this computer? The Scan page will also show you how to
echo   add the "Klick Klack" profile in ScanSnap Home (one-time step).
echo.
pause
