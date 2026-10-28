@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

rem ---------------------------------------------------------------------------
rem  Source-mode launcher. Checks the local environment before starting:
rem    1. .venv exists?           -> no: how to create it
rem    2. venv's python usable?  -> no: rebuild the venv
rem    3. deps importable?       -> no: which ones + install command
rem
rem  ASCII ONLY. cmd.exe parses this file byte-wise with the OEM codepage
rem  while the file itself is UTF-8: a non-ASCII byte pair can contain ")"
rem  and terminate an if-block early, which makes the literally printed
rem  commands actually RUN. (Really happened: it pip-installed on its own.)
rem  Keep every message here in English; Chinese lives in README.md.
rem
rem  Build and webm conversion are plain python, by design:
rem      .venv\Scripts\python.exe build.py
rem      .venv\Scripts\python.exe make_pet_webm.py
rem ---------------------------------------------------------------------------

set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYEXE%" goto noenv

rem dependency probe: prints the missing module names, empty = all good
set "DEPPROBE=%TEMP%\_dshpet_deps_%RANDOM%.tmp"
"%PYEXE%" -c "import importlib.util as u; print(' '.join(n for n in ('PySide6','av','numpy','zstandard') if u.find_spec(n) is None))" > "%DEPPROBE%" 2>nul
if errorlevel 1 goto broken

set "MISSING="
set /p MISSING=<"%DEPPROBE%"
del "%DEPPROBE%" >nul 2>nul
if not "%MISSING%"=="" goto nodeps

"%PYEXE%" -u "%~dp0main.py" %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
    echo.
    echo   [exit %RC%] see the messages above.
    pause
)
exit /b %RC%


:noenv
echo.
echo   [env] .venv not found in this folder.
echo.
where python >nul 2>nul
if errorlevel 1 goto nopython
echo   Create it once, run these two lines here:
echo.
echo       python -m venv .venv
echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
goto tail_exit


:nopython
echo   No python on PATH either. Install Python 3.10+ (tick "Add python.exe
echo   to PATH" in the installer), then run these two lines here:
echo.
echo       python -m venv .venv
echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
goto tail_exit


:nodeps
echo.
echo   [env] .venv is missing: %MISSING%
echo.
echo   Install them here:
echo.
echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
goto tail_exit


:broken
echo.
echo   [env] .venv\Scripts\python.exe cannot run - the venv is broken
echo         (moved folder, or the creation was interrupted).
echo         Delete .venv and create it again:
echo.
echo       python -m venv .venv
echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
goto tail_exit


:tail_exit
echo.
pause
exit /b 1
