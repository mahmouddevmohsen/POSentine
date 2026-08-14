@echo off
rem ============================================================
rem  UPDATE_POSENTINE.bat - one-click POSentine updater
rem ============================================================
rem  HOW TO USE (operator):
rem    1. WHERE THIS FILE MUST LIVE: UPDATE_POSENTINE.bat and the
rem       install\ folder must sit INSIDE the live install folder
rem       (the one that contains config.json and state.json), next
rem       to the other scripts. The bat resolves install\update_agent.ps1
rem       relative to its own location, so a copy sitting loose in
rem       Downloads cannot work - it stops with a clear message.
rem    2. Copy the new release zip (posentine-<commit>.zip) into:
rem          C:\Users\Techno\Downloads\
rem    3. Double-click THIS file - the one inside the live folder.
rem    4. Read the final screen.
rem          UPDATE SUCCESS  - done. The agent is on the new code.
rem          UPDATE FAILED   - do not touch anything. Send
rem                            logs\updater.log to support.
rem
rem  All the real logic lives in install\update_agent.ps1. This
rem  file only sets two optional knobs, verifies the updater is
rem  reachable from the right folder, and runs it. cmd is a poor
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

rem ---- the updater must sit next to this file, in the live folder -
rem  %~dp0 is THIS file's folder. The bat is only usable when it and
rem  install\update_agent.ps1 live INSIDE the live install folder -
rem  the one that also contains config.json and state.json (config.json
rem  is never in a release zip, so its presence marks the live folder).
rem  Anything else means the bat was double-clicked from the wrong
rem  place. Stop with a clear message instead of a PowerShell error
rem  about a missing file. Goto-based on purpose: parenthesized blocks
rem  containing %~dp0 paths mis-parse if a folder name ever contains
rem  a closing parenthesis.
if exist "%~dp0install\update_agent.ps1" if exist "%~dp0config.json" goto :updater_ok

echo.
echo   This bat is not inside the live install folder, so the
echo   updater cannot run from here.
echo.
if not exist "%~dp0install\update_agent.ps1" goto :missing_ps1
goto :delivery_folder

:missing_ps1
echo   update_agent.ps1 was not found next to this file.
echo   Looked for:
echo     %~dp0install\update_agent.ps1
if exist "%~dp0posentine\install\update_agent.ps1" goto :hint_extracted
goto :show_fix

:hint_extracted
echo.
echo   A copy was found inside the extracted zip folder:
echo     %~dp0posentine\install\update_agent.ps1
goto :show_fix

:delivery_folder
echo   update_agent.ps1 is here, but config.json is missing -
echo   this looks like the extracted delivery folder, not the
echo   live install folder.
goto :show_fix

:show_fix
echo.
echo   Fix: copy UPDATE_POSENTINE.bat and the install\ folder into
echo   the live install folder (the one that contains config.json
echo   and state.json), then double-click the bat from there.
echo.
echo   Nothing was run and nothing was changed.
pause
exit /b 1

:updater_ok
rem ---- OPTIONAL knobs -----------------------------------------
rem  ZIP_NAME     - pin ONE artifact by name, e.g.
rem                 set "ZIP_NAME=posentine-d9e27b6a98ce.zip"
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
set "EXPECTED_SHA=5629db77b9f5bff5b276aa1d9e329f8a5688c5d35b56a91d6a97fba6f7459659"

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
