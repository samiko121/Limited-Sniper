@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY% -m venv .venv || goto :error
)

rem Install / update dependencies only when requirements.txt changed
fc /b requirements.txt ".venv\installed-requirements.txt" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --require-hashes -r requirements.txt || goto :error
    copy /y requirements.txt ".venv\installed-requirements.txt" >nul
)

".venv\Scripts\python.exe" main.py %*
pause
exit /b

:error
echo.
echo Setup failed. Make sure Python 3.11 or newer is installed: https://www.python.org/downloads/
pause
exit /b 1
