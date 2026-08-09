@echo off
rem ============================================================
rem  collect_diagnostics.bat - one click, one file
rem ============================================================
rem  Double-click this when something looks wrong. It produces a
rem  single zip next to this file containing everything needed to
rem  diagnose the machine from somewhere else: the install
rem  transcripts, the agent logs, versions, ODBC drivers, the
rem  scheduled task, the local watermark, what is in the cloud,
rem  and a fresh proof that the POS still refuses our writes.
rem
rem  It reads only. It changes nothing.
rem
rem  The zip contains NO password and NO token. config.json is not
rem  included; a redacted copy is.
rem ============================================================

setlocal
title POSentine diagnostics

chcp 65001
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

echo.
echo ==================================================================
echo   POSentine - collect diagnostics
echo ==================================================================
echo   Folder: %CD%
echo.

where python >nul 2>nul
if errorlevel 1 goto no_python
python -c "import sys" >nul 2>nul
if errorlevel 1 goto no_python

python "%~dp0collect_diagnostics.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo   Done. Send the diagnostics_*.zip file listed above.
) else (
  echo   The archive could not be built. Read the error above.
)
echo.
pause
exit /b %RC%

:no_python
echo.
echo   No usable "python" on PATH, so the diagnostics cannot be built.
echo.
echo   Send these by hand instead, from this folder:
echo     agent.log
echo     the newest file in the logs folder
echo.
pause
exit /b 1
