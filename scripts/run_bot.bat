@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

if /i "%~1"=="--check" goto check

:loop
echo [%date% %time%] starting bot
py -3 scripts\run_bot_entry.py
set CODE=%ERRORLEVEL%
if "%CODE%"=="0" goto done
echo [%date% %time%] bot exited with code %CODE%, restart in 10s
timeout /t 10 /nobreak >nul
goto loop

:done
echo [%date% %time%] bot stopped by user
exit /b 0

:check
echo === environment check ===
py -3 scripts\env_check.py
exit /b %ERRORLEVEL%
