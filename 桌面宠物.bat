@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "VENV_ACTIVATE=%~dp0.venv\Scripts\activate.bat"
if exist "%VENV_ACTIVATE%" (
    call "%VENV_ACTIVATE%"
    echo 已启用项目 .venv
) else (
    echo 未找到项目 .venv，改用系统 PATH 里的 Python
)

python --version >nul 2>&1
if errorlevel 1 (
    echo 未检测到 Python，请安装 Python 3.12 并勾选 Add to PATH
    if /I not "%PET_NO_PAUSE%"=="1" pause
    exit /b 1
)

if not exist "%~dp0.env" (
    if exist "%~dp0.env.example" (
        copy /y "%~dp0.env.example" "%~dp0.env" >nul
        echo 已生成 .env，如需完整 AI 功能请填 API Key
    )
)

echo 正在检查 / 安装依赖（已装会自动跳过）...
python -m pip install -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo 清华镜像安装失败，改用官方 PyPI 重试...
    python -m pip install -r "%~dp0requirements.txt"
)
if errorlevel 1 (
    echo 依赖安装失败，请检查网络后重新双击本文件
    if /I not "%PET_NO_PAUSE%"=="1" pause
    exit /b 1
)

echo 正在启动桌宠...
python "%~dp0main.py"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo 桌宠已退出，退出码 %EXIT_CODE%
if /I not "%PET_NO_PAUSE%"=="1" pause
exit /b %EXIT_CODE%
