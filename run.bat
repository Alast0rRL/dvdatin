@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo [*] Using v2rayN proxy...
python "%~dp0main.py"
echo.
echo Press any key to exit...
pause >nul
