@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch.ps1" %*
set "launcherExitCode=%ERRORLEVEL%"
if not "%launcherExitCode%"=="0" pause
exit /b %launcherExitCode%
