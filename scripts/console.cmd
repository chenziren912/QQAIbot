@echo off
setlocal

rem PowerShell may block .ps1 files before the script can print diagnostics.
rem Use a process-only policy bypass and pass all console options through.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0console.ps1" %*
set "exit_code=%ERRORLEVEL%"
if not "%exit_code%"=="0" (
  echo.
  echo 终端控制台启动失败，退出代码：%exit_code%
)
exit /b %exit_code%
