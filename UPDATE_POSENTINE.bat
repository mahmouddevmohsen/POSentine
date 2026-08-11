@echo off
rem ============================================================
rem  UPDATE_POSENTINE.bat - one-click POSentine updater
rem ============================================================
rem  HOW TO USE (operator):
rem    1. Copy the new release zip (posentine-<commit>.zip) into:
rem          C:\Users\Techno\Downloads\
rem    2. Double-click this file.
rem    3. Read the final screen.
rem          UPDATE SUCCESS  - done. The agent is on the new code.
rem          UPDATE FAILED   - do not touch anything. Send
rem                            logs\updater.log to support.
rem
rem  All the real logic lives in install\update_agent.ps1. This
rem  file only sets two optional knobs and runs it. cmd is a poor
rem  language to be careful in; the care lives in PowerShell.
rem
rem  What the updater NEVER touches, no matter what:
rem    config.json  state.json  agent.log  logs\  the POS database
rem ============================================================

setlocal
title POSentine updater

rem The console must be UTF-8 before anything prints.
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

rem ---- OPTIONAL knobs -----------------------------------------
rem  ZIP_NAME     - pin ONE artifact by name, e.g.
rem                 set "ZIP_NAME=posentine-8d8bac091e9e.zip"
rem                 Empty = use the NEWEST posentine-*.zip found.
rem  EXPECTED_SHA - optional pin: the SHA-256 of the EXACT zip the
rem                 operator downloaded. Get it on the till with:
rem                   Get-FileHash ^<path to zip^> -Algorithm SHA256
rem                 Every rebuilt release has a NEW sha, so only paste
rem                 the hash of the file actually on that machine.
rem                 Empty = skip the pin; the updater still verifies
rem                 every shipped file against MANIFEST.txt, which is
rem                 the stronger gate.
set "ZIP_NAME="
set "EXPECTED_SHA="

rem ---- build the command line ---------------------------------
set "ARGS=-NoProfile -ExecutionPolicy Bypass -File "%~dp0install\update_agent.ps1" -DownloadsDir "C:\Users\Techno\Downloads""
if not "%ZIP_NAME%"=="" set "ARGS=%ARGS% -ZipName "%ZIP_NAME%""
if not "%EXPECTED_SHA%"=="" set "ARGS=%ARGS% -ExpectedSha256 "%EXPECTED_SHA%""

rem ---- run the updater ----------------------------------------
powershell %ARGS%
set "UPDATE_RC=%ERRORLEVEL%"

echo.
if "%UPDATE_RC%"=="0" (
  echo   UPDATE SUCCESS - see the final block above.
  echo   The agent is running the new code.
) else (
  echo   UPDATE FAILED - see the red block above.
  echo   Do not touch anything. Send logs\updater.log to support.
  echo   A backup of the previous code is kept in _backup\.
)
echo.
pause
exit /b %UPDATE_RC%
