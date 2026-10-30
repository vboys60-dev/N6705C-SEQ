@echo off
rem Double-click launcher for Windows 11. First run creates .venv and installs the package.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install -e . || goto :fail
)
".venv\Scripts\python.exe" -m n6705c_seq.webgui
if errorlevel 1 goto :fail
exit /b 0
:fail
echo.
echo Failed to start. Needs Python 3.11+ ("py" launcher) and internet on first run.
pause
