@echo off
chcp 65001 >nul
:: ============================================================
::  启动 BOSS 直聘采集服务（FastAPI + uvicorn）
::  双击本文件即可启动服务并自动打开浏览器页面。
::  依赖：Python 环境中已安装 fastapi / uvicorn（见 requirements.txt）。
::  前置：MySQL 需已运行（见 docker-compose.yml）。
::  关闭：直接关闭本窗口即可停止 Web 服务。
:: ============================================================
cd /d "%~dp0"

:: 加载 .env 中的数据库等配置（文件已被 gitignore 排除，不会提交）
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
        set "%%A=%%B"
    )
)

:: 仅激活项目自身的虚拟环境；不要激活上级 .venv（那是京东项目的，缺 openpyxl/websockets）
if exist "venv\Scripts\activate.bat" call "venv\Scripts\activate.bat"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

:: 优先使用已装齐依赖的系统 Python（D:\Python313），其次回退到 PATH 里的 python / py
if exist "D:\Python313\python.exe" (
    set "PYTHON=D:\Python313\python.exe"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        where py >nul 2>nul
        if errorlevel 1 (
            echo 错误：未找到 python，请先安装 Python 并勾选"加入 PATH"。
            pause
            exit /b 1
        )
        set PYTHON=py
    ) else (
        set PYTHON=python
    )
)

echo ============================================================
echo  正在启动 BOSS 直聘采集服务（FastAPI + uvicorn）...
echo  稍候会自动打开浏览器：http://127.0.0.1:8899
echo  启动完成后请勿关闭本窗口（关闭即停止服务）。
echo ============================================================
echo.

:: 等待服务起来后再打开浏览器，避免打开时服务尚未就绪
timeout /t 5 >nul
start "" "http://127.0.0.1:8899"

:: 前台运行 uvicorn（保持窗口打开以显示日志）
%PYTHON% -m uvicorn app:app --host 127.0.0.1 --port 8899
pause
