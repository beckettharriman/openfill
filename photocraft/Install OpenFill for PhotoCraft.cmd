@echo off
rem Install OpenFill for PhotoCraft for this Windows user: adds "PhotoCraft with OpenFill" to the
rem Start menu and the desktop. Run it again with --uninstall to remove it.
rem SPDX-License-Identifier: Apache-2.0
rem The folder is read before delayed expansion is on, so a ! in its path survives, and it
rem reaches Python through the environment, so an apostrophe in it is no Python syntax error.
setlocal DisableDelayedExpansion
set "OPENFILL_SRC=%~dp0"
setlocal EnableDelayedExpansion
rem python.org's install manager (py) may offer to install a Python and wait for an answer
rem nobody sees, inside for /f: it must not install anything, and gets no keyboard.
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PY="
rem An explicit ComfyUI Python takes priority, even without Tk.
if defined OPENFILL_PYTHON (
  "%OPENFILL_PYTHON%" -c "import sys, PIL; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
  if errorlevel 1 (
    echo OPENFILL_PYTHON must point to Python 3.10 or newer with Pillow.
    pause
    exit /b 1
  )
  set "PY=%OPENFILL_PYTHON%"
)
set "CHECK=import sys, PIL, tkinter; sys.exit(sys.version_info < (3, 10))"
for %%P in ("%OPENFILL_PYTHON%" "%USERPROFILE%\Documents\ComfyUI\.venv\Scripts\python.exe" "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%ProgramFiles%\Python314\python.exe" "%ProgramFiles%\Python313\python.exe" "%ProgramFiles%\Python312\python.exe" "%ProgramFiles%\Python311\python.exe") do (
  if not defined PY if exist "%%~P" "%%~P" -c "%CHECK%" >nul 2>&1 && set "PY=%%~P"
)
if not defined PY (
  where py >nul 2>&1 && for /f "delims=" %%E in ('py -3 -c "import sys, PIL, tkinter; print(sys.executable)" ^<nul 2^>nul') do set "PY=%%E"
)
rem The Python `py` starts: python.org's install manager (the default from Python 3.14) keeps
rem Python in its own folder and provides `py`.
set "PYEXE="
if not defined PY where py >nul 2>&1 && for /f "delims=" %%E in ('py -3 -c "import sys; print(sys.executable)" ^<nul 2^>nul') do set "PYEXE=%%E"
rem A Python with Tk but without Pillow (python.org's installer has Tk): add Pillow for this user.
if not defined PY (
  for %%P in ("%OPENFILL_PYTHON%" "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" "%ProgramFiles%\Python314\python.exe" "%ProgramFiles%\Python313\python.exe" "%ProgramFiles%\Python312\python.exe" "%ProgramFiles%\Python311\python.exe" "%PYEXE%") do (
    if not defined PY if exist "%%~P" (
      "%%~P" -c "import sys, tkinter; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && (
        echo Adding Pillow to %%~P ...
        "%%~P" -m pip install --user --quiet --disable-pip-version-check pillow && "%%~P" -c "%CHECK%" >nul 2>&1 && set "PY=%%~P"
      )
    )
  )
)
rem Without Tk the OpenFill panel opens as a tab in the user's default browser instead: ComfyUI's own Python works.
set "CHECK=import sys, PIL; sys.exit(sys.version_info < (3, 10))"
for %%P in ("%LOCALAPPDATA%\OpenFill\ComfyUI\ComfyUI_windows_portable\python_embeded\python.exe" "C:\AI\ComfyUI\ComfyUI_windows_portable\python_embeded\python.exe" "C:\ComfyUI_windows_portable\python_embeded\python.exe" "%USERPROFILE%\ComfyUI_windows_portable\python_embeded\python.exe" "%USERPROFILE%\Downloads\ComfyUI_windows_portable\python_embeded\python.exe" "%USERPROFILE%\Desktop\ComfyUI_windows_portable\python_embeded\python.exe") do (
  if not defined PY if exist "%%~P" "%%~P" -c "%CHECK%" >nul 2>&1 && set "PY=%%~P"
)
if not defined PY (
  echo OpenFill needs Python, and none was found on this PC.
  echo.
  echo 1. Install Python from python.org: the page opens now. The default options are fine.
  echo 2. Then double-click Install OpenFill for PhotoCraft.cmd again. It adds Pillow by itself.
  echo.
  echo Already have ComfyUI? Its Python works too: see Use ComfyUI's Python in README.md in this folder.
  if not defined OPENFILL_NO_BROWSER start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)
echo Using Python: !PY!
"!PY!" -c "import os, sys; sys.path.insert(0, os.path.normpath(os.environ['OPENFILL_SRC'])); from openfill_photocraft.install import main; raise SystemExit(main(sys.argv[1:]))" %*
pause
