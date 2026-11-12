@echo off
call "%~dp0Start-QA.cmd" register-key
exit /b %ERRORLEVEL%
