@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 用 pet_py 自带的 venv
set PYEXE=%~dp0.venv\Scripts\python.exe
if exist "%PYEXE%" goto run

echo.
echo   [未找到 .venv] 先跑一次 setup-pet.cmd 安装依赖。
echo.
pause
exit /b 1

:run
"%PYEXE%" -u "%~dp0main.py" %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
    echo.
    echo   [退出码 %RC%] 上面有报错信息。
    pause
)
exit /b %RC%
