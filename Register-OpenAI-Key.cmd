@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Project-QA.ps1" -RegisterKey
set "QA_EXIT=%ERRORLEVEL%"
pause
exit /b %QA_EXIT%
