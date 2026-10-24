@echo off
rem Install OpenFill for PhotoCraft for this Windows user: adds "PhotoCraft with OpenFill" to the
rem Start menu and the desktop. Run it again with --uninstall to remove it.
rem SPDX-License-Identifier: Apache-2.0
setlocal EnableDelayedExpansion
set "DIR=%~dp0"
set "PY="
set "CHECK=import sys, PIL, tkinter; sys.exit(sys.version_info < (3, 10))"
for %%P in ("%OPENFILL_PYTHON%" "%USERPROFILE%\Documents\ComfyUI\.venv\Scripts\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe") do (
  if not defined PY if exist "%%~P" "%%~P" -c "%CHECK%" >nul 2>&1 && set "PY=%%~P"
)
if not defined PY (
  where py >nul 2>&1 && for /f "delims=" %%E in ('py -3 -c "import sys, PIL, tkinter; print(sys.executable)" 2^>nul') do set "PY=%%E"
)
rem A Python with Tk but without Pillow (python.org's installer has Tk): add Pillow for this user.
if not defined PY (
  for %%P in ("%OPENFILL_PYTHON%" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%LOCALAPPDATA%\Programs\Python\Python310\python.exe") do (
    if not defined PY if exist "%%~P" (
      "%%~P" -c "import sys, tkinter; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && (
        echo Adding Pillow to %%~P ...
        "%%~P" -m pip install --user --quiet --disable-pip-version-check pillow && "%%~P" -c "%CHECK%" >nul 2>&1 && set "PY=%%~P"
      )
    )
  )
)
rem Without Tk the OpenFill panel opens as a small Edge window instead: ComfyUI's own Python works.
set "CHECK=import sys, PIL; sys.exit(sys.version_info < (3, 10))"
for %%P in ("%LOCALAPPDATA%\OpenFill\ComfyUI\ComfyUI_windows_portable\python_embeded\python.exe" "C:\AI\ComfyUI\ComfyUI_windows_portable\python_embeded\python.exe" "C:\ComfyUI_windows_portable\python_embeded\python.exe" "%USERPROFILE%\ComfyUI_windows_portable\python_embeded\python.exe" "%USERPROFILE%\Downloads\ComfyUI_windows_portable\python_embeded\python.exe" "%USERPROFILE%\Desktop\ComfyUI_windows_portable\python_embeded\python.exe") do (
  if not defined PY if exist "%%~P" "%%~P" -c "%CHECK%" >nul 2>&1 && set "PY=%%~P"
)
if not defined PY (
  echo OpenFill needs Python 3.10 or newer with Pillow: ComfyUI's own Python has it.
  echo Install ComfyUI first ^(OpenFill for GIMP can download it^), or set OPENFILL_PYTHON.
  pause
  exit /b 1
)
echo Using Python: !PY!
"!PY!" -c "import sys; sys.path.insert(0, r'%DIR:~0,-1%'); from openfill_photocraft.install import main; raise SystemExit(main(sys.argv[1:]))" %*
pause
