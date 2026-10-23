@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 创建 .venv ...
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
)
if not exist ".venv\Scripts\python.exe" (
    echo   [失败] 找不到可用的 Python 3，请先安装 Python 3.10+ 并加入 PATH。
    pause
    exit /b 1
)

echo [2/3] 安装依赖（腾讯云镜像）...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -i https://mirrors.cloud.tencent.com/pypi/simple -r requirements.txt
if errorlevel 1 (
    echo   [失败] 依赖安装出错，检查网络或换源。
    pause
    exit /b 1
)

echo [3/3] 自检 ...
".venv\Scripts\python.exe" -c "import PySide6, av, numpy; print('  PySide6', PySide6.__version__, '| av', av.__version__, '| numpy', numpy.__version__)"
if errorlevel 1 (
    echo   [失败] 导入失败。
    pause
    exit /b 1
)

echo.
echo   就绪。双击 run-pet.cmd 启动桌宠。
echo.
pause
