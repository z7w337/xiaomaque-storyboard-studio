@echo off
rem Storyboard Studio - one-click launcher (final, v7-2)
rem Path weak-coupling: %~dp0 derives own dir (movable/renamable)
chcp 65001 >nul
set "BASE=%~dp0"
cd /d "%BASE%"

rem ===== 派单001 T1：本版本**强制仅本机**（main.py 里 host 固定 127.0.0.1，不读 settings.listen）=====
rem 仍读一下配置：**只为**在"配置写了 0.0.0.0"时给醒目提示（让人知道为什么不生效）
set "LAN=0"
if exist "%BASE%data\settings.json" (
  findstr /C:"0.0.0.0" "%BASE%data\settings.json" >nul 2>&1 && set "LAN=1"
)

echo ============================================
echo  Storyboard Studio - 一键启动
echo ============================================
if "%LAN%"=="1" (
  echo  [注意] 配置里写了 0.0.0.0，但本版本强制仅本机监听，^^!^^! 该配置不生效 ^^!^^!
  echo  [监听模式] 仅本机 ^(127.0.0.1^)  — 开放局域网需先修完 P0-2 三条（见 main.py 启动段注释）
) else (
  echo  [监听模式] 仅本机 ^(127.0.0.1^)  — 本版本不开放局域网
)
echo  访问: http://127.0.0.1:3100/
echo  按 Ctrl+C 停止服务.
echo.
echo  [启动] 正在启动服务与打开浏览器...
echo.

rem 浏览器后台延迟打开（无论监听模式，本机访问 127.0.0.1 始终可达）
start /b cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:3100/"
rem ==== Python interpreter probe (dispatch002 T5-3) ====
rem  1) prefer bundled  : %BASE%python\python.exe        (release layout)
rem  2) else fallback  : %BASE%..\storyboard-studio\python\python.exe (dev layout) + notice
rem  3) neither        : loud error + pause + exit /b 1   (never fail silently)
rem  NOTE: keep this block ASCII-only -- multibyte text here corrupts cmd parsing.
set "PY="
if exist "%BASE%python\python.exe" (
  set "PY=%BASE%python\python.exe"
) else if exist "%BASE%..\storyboard-studio\python\python.exe" (
  set "PY=%BASE%..\storyboard-studio\python\python.exe"
  echo  [提示] 未找到随包 python\python.exe，回退开发机解释器
) else (
  echo  ============================================
  echo   [错误] 找不到 Python 解释器，无法启动
  echo     发布态期望：%BASE%python\python.exe
  echo     开发态期望：%BASE%..\storyboard-studio\python\python.exe
  echo   请确认发布包完整（应含 python\ 目录），或在开发机上运行。
  echo  ============================================
  pause
  exit /b 1
)
"%PY%" main.py

echo.
echo ============================================
echo  服务已停止.
echo ============================================
pause
