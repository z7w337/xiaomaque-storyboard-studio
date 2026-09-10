@echo off
rem 小麻雀剧本分镜工坊 - one-click launcher
rem Path weak-coupling: %~dp0 derives own dir (movable/renamable)
chcp 65001 >nul
set "BASE=%~dp0"
cd /d "%BASE%"

rem ===== 监听地址设置开关：读 data\settings.json 判断局域网模式（默认仅本机）=====
rem 匹配 "0.0.0.0"（settings.json 中该值仅用于 listen 的局域网选项，避引号转义坑）
set "LAN=0"
if exist "%BASE%data\settings.json" (
  findstr /C:"0.0.0.0" "%BASE%data\settings.json" >nul 2>&1 && set "LAN=1"
)

echo ============================================
echo  小麻雀剧本分镜工坊 - 一键启动
echo ============================================
if "%LAN%"=="1" (
  echo  [监听模式] 局域网可见 ^(0.0.0.0^)  ^^!^^! 请确认网络环境安全后再继续 ^^!^^!
  echo  [提示] 局域网内其他设备可访问本机 3100 端口；仅需本机时请在设置页切回"仅本机 127.0.0.1"。
) else (
  echo  [监听模式] 仅本机 ^(127.0.0.1^)  — 局域网访问请在设置页开启"局域网 0.0.0.0"开关
)
echo  访问: http://127.0.0.1:3100/
echo  按 Ctrl+C 停止服务.
echo.

rem ===== 解释器解析：优先本目录内嵌 python\，否则回退系统 Python =====
set "PYEXE="
if exist "%BASE%python\python.exe" set PYEXE="%BASE%python\python.exe"
if not defined PYEXE where python >nul 2>&1 && set "PYEXE=python"
if not defined PYEXE where py >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE (
  echo  [错误] 未找到 Python 解释器。
  echo         请安装 Python 3.10+ 并加入 PATH；
  echo         或在本目录放置内嵌解释器 python\python.exe 后重试。
  echo.
  pause
  exit /b 1
)

echo  [启动] 使用解释器: %PYEXE%
echo  [启动] 正在启动服务与打开浏览器...
echo.

rem 浏览器后台延迟打开（无论监听模式，本机访问 127.0.0.1 始终可达）
start /b cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:3100/"
%PYEXE% main.py

echo.
echo ============================================
echo  服务已停止.
echo ============================================
pause
