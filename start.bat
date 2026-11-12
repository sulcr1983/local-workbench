@echo off
rem 本文件是 GBK(cp936) + CRLF 编码，专为 cmd 准备：切勿改成 UTF-8 或 LF，
rem 否则 cmd 会按字节偏移错位解析，中文行后的命令被截断成乱码报错。
title Supersu 工作台
cd /d "%~dp0"

echo.
echo   Supersu 工作台 · 本地项目与云资源管理
echo   ---------------------------------------------
echo.

set "VENV_PY=.venv\Scripts\python.exe"

rem 本窗口就是工作台的控制台：日志留在这里，关窗即停止。
rem 启动的进程不加 WB_NO_BROWSER，页面由 app.py 自己拉起默认浏览器。

rem ============================================================
rem  1. 找 Python / 建虚拟环境（只建一次）
rem ============================================================
if exist "%VENV_PY%" goto CHECK_DEPS

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto NO_PYTHON

echo   首次运行，正在创建运行环境（只做这一次）...
%PY% -m venv .venv >nul 2>&1
if not exist "%VENV_PY%" goto VENV_FAIL

rem ============================================================
rem  2. 装依赖：缺才装，装不上就明确失败（绝不带病启动）
rem ============================================================
rem  探测清单 = 实测得出，必须覆盖 app.py 启动时必然加载的全部三方包。
rem  两家云 SDK（alibabacloud_ecs20140526 / huaweicloudsdkecs）不探测：
rem  它们只在 wb/expiry/providers/aliyun.py、huawei.py 函数内延迟导入，
rem  缺了不该拦住工作台启动。改 requirements.txt 后要顺手核一遍这里。
:CHECK_DEPS
"%VENV_PY%" -c "import flask, waitress, paramiko, requests, apscheduler, markdown, nh3, tencentcloud" >nul 2>&1
if not errorlevel 1 goto START

echo   正在安装依赖，首次约 1-3 分钟（能看到下载进度就说明正常）...
echo.
"%VENV_PY%" -m pip install --disable-pip-version-check --timeout 20 --retries 2 -r requirements.txt
if not errorlevel 1 goto START

echo.
echo   [!] 默认源没装上，换清华镜像再试一次...
echo.
"%VENV_PY%" -m pip install --disable-pip-version-check --timeout 20 --retries 2 -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 goto PIP_FAIL

rem ============================================================
rem  3. 启动
rem ============================================================
:START
echo.
echo   正在启动，浏览器会自动打开  http://127.0.0.1:8787
echo.
"%VENV_PY%" app.py
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="2" (
  echo   工作台已经在运行中，直接看浏览器就好。
) else (
  echo   工作台已退出。
)
pause
exit /b 0

rem ============================================================
rem  出错分支
rem ============================================================
:NO_PYTHON
echo   [!] 没有找到 Python。
echo.
echo   请先装 Python 3.10 或更高版本，安装时务必勾选 "Add Python to PATH"：
echo       https://www.python.org/downloads/
echo.
start "" https://www.python.org/downloads/
pause
exit /b 1

:VENV_FAIL
echo   [!] 创建虚拟环境失败，通常是 Python 装得不完整（缺 venv 组件）。
echo       重装 Python 时勾上 "pip" 和 "Add Python to PATH"，然后重试。
pause
exit /b 1

:PIP_FAIL
echo   [!] 依赖没装上，工作台跑不起来（这不是能凑合的情况）。
echo.
echo   手动重试看看具体报错：
echo       .venv\Scripts\python -m pip install -r requirements.txt
echo.
echo   常见原因：没网 / 公司代理挡了 pip / 磁盘满了。
pause
exit /b 1
