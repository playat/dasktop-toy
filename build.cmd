@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 默认：目录版 dist\DshPet\DshPet.exe
rem 单文件：build.cmd --onefile
rem 带控制台看日志：build.cmd --console
rem 全清重来：build.cmd --clean

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   [缺少运行环境] 先跑一次 setup-pet.cmd
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" build.py %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo   [退出码 %RC%] 上面有报错信息。
pause
exit /b %RC%
