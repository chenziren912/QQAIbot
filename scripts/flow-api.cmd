@echo off
setlocal

rem Manually start the Google Flow API watchdog.  The watchdog starts the
rem Node API when needed and restarts it after a crash.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0flow-api-guard.ps1" %*
set "exit_code=%ERRORLEVEL%"
if not "%exit_code%"=="0" (
  echo.
  echo Flow API 守护启动失败，退出代码：%exit_code%
)
exit /b %exit_code%
