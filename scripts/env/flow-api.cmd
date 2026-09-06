@echo off
setlocal

rem Compatibility wrapper for the Flow watchdog shipped in the parent scripts
rem directory.  This keeps old env-package shortcuts on the same implementation.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\flow-api-guard.ps1" %*
set "exit_code=%ERRORLEVEL%"
if not "%exit_code%"=="0" (
  echo.
  echo Flow API 守护启动失败，退出代码：%exit_code%
)
exit /b %exit_code%
