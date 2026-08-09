@echo off
rem ============================================================
rem  INSTALL.bat - POSentine, the whole install in one double-click
rem ============================================================
rem  Double-click this. It runs VERIFY.md steps 1 to 8 as gated
rem  phases, stops at the first failure, and tells you which step
rem  failed and what to do.
rem
rem  Phase A reads only. Nothing is written to the POS database or
rem  to the cloud until Phase A has passed. The POS database is
rem  never written to at any point - Phase A proves that by
rem  attempting to write to it and requiring every attempt to be
rem  refused.
rem
rem  It takes about 10 minutes, most of which is Phase E waiting
rem  for the scheduled task to fire on its own. That wait is the
rem  point: it is the only thing that proves the agent keeps
rem  working after we leave.
rem
rem  Safe to run twice. Run it again if you are not sure it worked.
rem
rem  This file stays deliberately thin. Everything that needs a
rem  judgement lives in installer.py, where it is covered by tests.
rem  cmd is a poor language to be careful in.
rem ============================================================

setlocal
title POSentine install

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
echo   POSentine install
echo ==================================================================
echo   Folder: %CD%
echo.
echo   This will take about 10 minutes. Most of it is a wait, on
echo   purpose. Do not close this window.
echo.

rem Python is checked here rather than in installer.py for the obvious
rem reason: without it, installer.py cannot run to report its absence.
where python >nul 2>nul
if errorlevel 1 goto no_python

rem `where python` also matches the Windows Store stub, which is not
rem Python and exits non-zero the moment it is asked to do anything.
python -c "import sys" >nul 2>nul
if errorlevel 1 goto no_python

python "%~dp0installer.py" %*
set "INSTALL_RC=%ERRORLEVEL%"

echo.
if "%INSTALL_RC%"=="0" (
  echo   ==============================================================
  echo   INSTALLED. The agent is running and has proved it.
  echo   ==============================================================
  echo   Read the summary above before you leave.
) else (
  echo   ==============================================================
  echo   STOPPED. Read the block above before doing anything else.
  echo   ==============================================================
  echo   Photograph this screen. Change nothing on this machine.
  echo   The full log is in the logs folder next to this file.
)
echo.
pause
exit /b %INSTALL_RC%

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
echo   No scheduled task was registered.
echo ==================================================================
echo.
pause
exit /b 1
