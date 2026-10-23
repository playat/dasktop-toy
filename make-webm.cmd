@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 用法一：把 AI 视频文件夹拖到本文件上（默认 --deploy 会问你）
rem 用法二：make-webm.cmd "D:\AI视频" --deploy --jobs 8

set "PYEXE=pet_py\.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
    echo.
    echo   [缺少运行环境] 先跑一次 pet_py\setup-pet.cmd
    echo.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo.
    echo   把 AI 绿幕视频文件夹拖到本文件上，或者：
    echo     make-webm.cmd "D:\AI视频" [--deploy] [--no-watermark] [--jobs 8]
    echo.
    pause
    exit /b 1
)

set "SRC=%~1"
echo.
echo   源目录：%SRC%
echo.
"%PYEXE%" make_pet_webm.py --src "%SRC%" %2 %3 %4 %5 %6 %7 %8 %9
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo   [退出码 %RC%] 上面有报错信息。
pause
exit /b %RC%
