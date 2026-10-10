@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0enable_1m.py" %*
) else (
    python "%~dp0enable_1m.py" %*
)
set "setup_exit=%errorlevel%"
echo.
pause
exit /b %setup_exit%
