@echo off
setlocal
cd /d "%~dp0"
python tools\setup.py
if errorlevel 1 (
    echo.
    echo Setup failed. Fix the issues above and run setup.bat again.
    pause
    exit /b 1
)
echo.
echo Done.
pause
