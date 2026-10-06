@echo off
rem JAB optical file sender launcher (runs inside a local venv)
setlocal
cd /d "%~dp0"
set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"

if exist "%VPY%" goto deps
echo [setup] Creating virtual environment in .venv ...
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo Python 3 was not found. Install it from https://www.python.org
  pause
  exit /b 1
)
%PY% -m venv "%VENV%"
if not exist "%VPY%" (
  echo Failed to create the virtual environment.
  pause
  exit /b 1
)

:deps
rem Reinstall packages only when requirements.txt changed
fc /b "sender\requirements.txt" "%VENV%\requirements.installed" >nul 2>nul
if errorlevel 1 (
  echo [setup] Installing packages into .venv ...
  "%VPY%" -m pip install --upgrade pip
  "%VPY%" -m pip install -r "sender\requirements.txt"
  if errorlevel 1 (
    echo Package installation failed.
    pause
    exit /b 1
  )
  copy /y "sender\requirements.txt" "%VENV%\requirements.installed" >nul
)

cd /d "%~dp0sender"
"%VPY%" jab_sender.py %*
if errorlevel 1 pause
endlocal
