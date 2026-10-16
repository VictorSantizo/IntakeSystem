@echo off
REM server_next_visit.bat -- the "Server console, next visit" punch list.
REM REWRITTEN 2026-07-24 (Swan Cabinets CK#3012 incident): the 07-18 list is DONE
REM (51/346 merged, reports 2 3 4 6 resolved, OCR backlog cleared, #273 amount fixed).
REM Run ON THE SERVER (LACKSSERVER2021), from this folder.
cd /d "%~dp0"
set PYTHON=%~dp0python\python.exe

echo ==================================================================
echo  1) Midday catch-up runs for harvest + reconcile (Swan incident:
echo     a paid bill filed at 11:15 AM sat invisible until the next
echo     6 AM sweep). Two extra daily runs: 12:00 and 16:30.
echo     First CHECK the existing 6 AM task's name/account so these match:
echo ==================================================================
schtasks /query /fo LIST /v | findstr /i "TaskName harvest reconcile klick"
echo.
echo   If the query above looks right, create the two tasks (as SYSTEM,
echo   same as the 6 AM task):
echo.
set OPS=F:\File-Cabinet\_OPS INDEX
set RUNPAIR=cmd /c cd /d "%%OPS%%" ^&^& "%~dp0python\python.exe" harvest_daily_filing.py --apply ^&^& "%~dp0python\python.exe" reconcile_payments.py --apply
echo   schtasks /create /tn "Klick Klack noon filing catch" /tr "%RUNPAIR%" /sc daily /st 12:00 /ru SYSTEM
echo   schtasks /create /tn "Klick Klack 4:30 filing catch" /tr "%RUNPAIR%" /sc daily /st 16:30 /ru SYSTEM
echo.
echo   (Commands are ECHOED, not run -- copy/paste after checking the query.)

echo.
echo ==================================================================
echo  2) Optional cleanups while here
echo ==================================================================
echo   FAIR Plan byte-identical Legal copies (all Pending, no payments):
echo     "%PYTHON%" app.py merge-record 115 114
echo     "%PYTHON%" app.py merge-record 204 114
echo     "%PYTHON%" app.py merge-record 229 114
echo   (or one click on the Legacy Duplicates admin page from any browser)

echo.
echo ==================================================================
echo  3) Reports -- resolve only after a quick eyeball in the app
echo ==================================================================
echo   Verified in the data already: #7 (Home Depot statement = record #510),
echo   #8 (source-email panel live), #11 (Goleta invoices #554/#555 + #273 fixed):
echo     "%PYTHON%" app.py resolve-report 7 8 11
echo   After click-checks only: #5 #9 #10 (see reports triage 2026-07-23).
echo   Scan-into-Klick-Klack (#23 #18 #13): after ONE successful test scan
echo   from Karen's front machine (SCANNER_SETUP.md):
echo     "%PYTHON%" app.py resolve-report 23 18 13

echo.
pause
