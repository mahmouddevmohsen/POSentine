@echo off
rem ============================================================
rem  preflight.bat - POSentine, VERIFY.md steps 1 to 4
rem ============================================================
rem  Double-click this. It runs the four checks that decide whether
rem  this install is safe, stops at the first failure, and tells you
rem  which VERIFY.md step failed and what to do.
rem
rem  Steps 1-4 read only. Nothing here writes to the POS database or
rem  to the cloud, which is what makes one click carry no risk.
rem  Steps 5 onward stay manual - those involve decisions.
rem
rem  This file stays deliberately thin. Everything that needs a
rem  judgement lives in preflight.py, where it is covered by tests.
rem  cmd is a poor language to be careful in.
rem ============================================================

setlocal
title POSentine preflight

rem The console must be UTF-8 before anything prints: the item names,
rem and several of the agent's own error messages, are Arabic.
chcp 65001
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem Run from the folder this file sits in, so a double-click from
rem anywhere behaves the same as running it from a prompt.
cd /d "%~dp0"

echo.
echo ==================================================================
echo   POSentine preflight
echo ==================================================================
echo   Folder: %CD%
echo.

rem Python is checked here rather than in preflight.py for the obvious
rem reason: without it, preflight.py cannot run to report its absence.
where python >nul 2>nul
if errorlevel 1 goto no_python

rem `where python` also matches the Windows Store stub, which is not
rem Python and exits non-zero the moment it is asked to do anything.
python -c "import sys" >nul 2>nul
if errorlevel 1 goto no_python

python "%~dp0preflight.py" %*
set "PREFLIGHT_RC=%ERRORLEVEL%"

echo.
if "%PREFLIGHT_RC%"=="0" (
  echo   preflight finished: steps 1-4 PASSED.
  echo   Continue with VERIFY.md step 5, on this screen.
) else (
  echo   preflight STOPPED. Read the block above before doing anything else.
  echo   Nothing was written to the POS or to the cloud.
)
echo.
pause
exit /b %PREFLIGHT_RC%

:no_python
echo.
echo ==================================================================
echo   STOPPED at VERIFY.md step 1 - console and Python
echo ==================================================================
echo.
echo   What failed:
echo     No usable "python" on PATH.
echo.
echo   What to do:
echo     Install Python 3.11 or 3.12 from python.org.
echo     Tick "Add python.exe to PATH" during the install.
echo     Close this window, open a new one, and run this file again.
echo.
echo     If Python IS installed, the Store stub may be shadowing it:
echo     Settings, App execution aliases, turn off both "python" entries.
echo.
echo   Nothing was written to the POS or to the cloud.
echo ==================================================================
echo.
pause
exit /b 1
