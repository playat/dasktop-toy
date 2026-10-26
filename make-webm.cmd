@echo off
rem ---------------------------------------------------------------------------
rem  Usage (ASCII only on purpose: a UTF-8 batch file gets mis-parsed by cmd.exe,
rem  which can even mangle the arguments you pass in):
rem
rem    1) drop your green-screen mp4 into  video\src  and double-click this file
rem       -> converts whatever is missing, skips what already exists
rem       -> output goes straight to  assets\webm  (no copy/sync step)
rem    2) drag a folder onto this file:  make-webm.cmd "D:\AI_videos"
rem    3) flags only:                   make-webm.cmd --jobs 8
rem
rem  Sub-folders are supported and mirrored:
rem       video\src\eat\dumpling.mp4  ->  assets\webm\eat\dumpling.webm
rem  Missing output folders are created for you. Clean up assets\webm yourself.
rem
rem  All user-facing Chinese text is printed by the Python script, not here.
rem ---------------------------------------------------------------------------
cd /d "%~dp0"

set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" goto novenv

if "%~1"=="" goto default
if exist "%~1\" goto fromdir
"%PYEXE%" make_pet_webm.py %*
goto done

:fromdir
"%PYEXE%" make_pet_webm.py --src "%~1" %2 %3 %4 %5 %6 %7 %8 %9
goto done

:default
"%PYEXE%" make_pet_webm.py
goto done

:done
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   [exit %RC%] see the messages above.
pause
exit /b %RC%

:novenv
echo.
echo   [missing] .venv not found - run setup-pet.cmd first.
echo.
pause
exit /b 1
