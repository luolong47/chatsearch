@echo off
chcp 65001 >nul
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 uv，请先安装：https://docs.astral.sh/uv/
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [初始化] 首次运行，正在创建虚拟环境并安装依赖...
    uv sync
)

uv run python -c "import pystray, PIL" >nul 2>&1
if errorlevel 1 (
    echo [初始化] 安装托盘依赖...
    uv sync
)

echo [启动] 托盘图标已加入通知区域
echo        双击图标打开检索页面；关托盘请在图标右键菜单选「退出」
start "" "%~dp0.venv\Scripts\pythonw.exe" -m chatsearch.tray
timeout /t 2 >nul
exit /b 0
