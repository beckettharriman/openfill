@echo off
rem OpenFill installer: double-click this file. It runs install.ps1 and waits for a key at the end.
rem SPDX-License-Identifier: Apache-2.0
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RESULT=%ERRORLEVEL%
echo.
if not "%RESULT%"=="0" echo The installer stopped with an error (see the message above).
pause
exit /b %RESULT%
