@echo off
REM run_catchup_now.bat -- run the harvest + reconcile catch-up pair RIGHT NOW.
REM Double-click ON THE SERVER (LACKSSERVER2021), e.g. over RDP:
REM   F:\File-Cabinet\IntakeSystem\run_catchup_now.bat
REM Safe by design: both scripts write intake.db ONLY when they detect they are
REM running on the server (F:\File-Cabinet exists); anywhere else they dry-run.
set PYTHON=%~dp0python\python.exe
set OPS=%~dp0..\_OPS INDEX

echo ================= HARVEST (manual filings -> intake) =================
"%PYTHON%" "%OPS%\harvest_daily_filing.py" --apply
echo.
echo ================= RECONCILE (payments -> mark bills paid) ============
"%PYTHON%" "%OPS%\reconcile_payments.py" --apply
echo.
echo Reports written: "%OPS%\HARVEST.html" and "%OPS%\RECONCILIATION.html"
echo (check the new "Held for a human" and "invisible - blank amount" sections)
echo.
echo ============ OPTIONAL: midday cadence (needs an ADMIN prompt) ========
echo To stop morning filings waiting for the next 6 AM, create the two
echo catch-up tasks -- run these in an elevated prompt ON the server:
echo.
echo schtasks /create /tn "Klick Klack noon filing catch" /sc daily /st 12:00 /ru SYSTEM /tr "cmd /c cd /d F:\File-Cabinet\_OPS INDEX && F:\File-Cabinet\IntakeSystem\python\python.exe harvest_daily_filing.py --apply && F:\File-Cabinet\IntakeSystem\python\python.exe reconcile_payments.py --apply"
echo schtasks /create /tn "Klick Klack 4:30 filing catch" /sc daily /st 16:30 /ru SYSTEM /tr "cmd /c cd /d F:\File-Cabinet\_OPS INDEX && F:\File-Cabinet\IntakeSystem\python\python.exe harvest_daily_filing.py --apply && F:\File-Cabinet\IntakeSystem\python\python.exe reconcile_payments.py --apply"
echo.
pause
