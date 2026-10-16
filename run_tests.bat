@echo off
REM Klickity Klack test suite. Runs against TEMP databases/configs only —
REM completely safe to run on the production server at any time.
cd /d "%~dp0"
python\python.exe -m pytest tests -q %*
