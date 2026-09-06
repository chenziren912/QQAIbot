@echo off
setlocal

rem Compatibility entry point for packages that still launch scripts\env.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0console.ps1" %*
set "exit_code=%ERRORLEVEL%"
if not "%exit_code%"=="0" (
  echo.
  echo 终端控制台启动失败，退出代码：%exit_code%
)
exit /b %exit_code%
