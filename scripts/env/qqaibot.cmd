@echo off
setlocal

rem PowerShell may block .ps1 files before the script can print its own
rem diagnostics.  This wrapper applies a process-only bypass and forwards all
rem arguments, without changing the user's machine-wide execution policy.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "exit_code=%ERRORLEVEL%"
if not "%exit_code%"=="0" (
  echo.
  echo 启动失败，退出代码：%exit_code%
)
exit /b %exit_code%
