@echo off
chcp 65001 >nul 2>nul
rem 入口放在大脑文件夹，脚本本体仍在 whale-girl 目录：
rem 桥接要读那边的 config.json 和 data/，所以工作目录必须切过去。
set "WG=C:\Users\lenovo\Desktop\whale-girl-3.0-full"
set "PY=%WG%\runtime\python\python.exe"
if not exist "%PY%" set "PY=python"
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
cd /d "%WG%"
if exist "data\PAUSED" del /q "data\PAUSED" >nul 2>&1

echo.
echo   ==========================================
echo     WeChat bridge  -^>  muyu brain (8790)
echo   ==========================================
echo.
echo   Need: WeChat 4.x running and logged in.
echo   Need: brain window started first (port 8790).
echo.
echo   Press Ctrl+C to stop.   Python: %PY%
echo.

setlocal enabledelayedexpansion
set /a RESTARTS=0

:loop
"%PY%" -u "%WG%\examples\wechatauto_bridge.py"
set "CODE=%ERRORLEVEL%"

if "%CODE%"=="2" (
  set /a RESTARTS+=1
  if !RESTARTS! GEQ 5 (
    echo.
    echo   [!] Restarted 5 times and still stuck.
    echo       Check WeChat window: must be visible, not minimized to tray.
    pause
    exit /b 2
  )
  echo   [wd] killed by watchdog, restarting in 5s ^(retry !RESTARTS!/5^)
  timeout /t 5 /nobreak >nul
  goto loop
)

echo.
echo Bridge exited with code %CODE%.
pause >nul
exit /b %CODE%
