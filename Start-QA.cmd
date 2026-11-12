@echo off
setlocal
pushd "%~dp0" || exit /b 1

set "QA_PYTHON=%LOCALAPPDATA%\Python\bin\python.exe"
if exist "%QA_PYTHON%" (
    "%QA_PYTHON%" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 goto run
)

set "QA_PYTHON=python.exe"
"%QA_PYTHON%" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto run

set "QA_PYTHON=%ProgramFiles%\Epic Games\UE_5.9\Engine\Binaries\ThirdParty\Python3\Win64\python.exe"
if exist "%QA_PYTHON%" (
    "%QA_PYTHON%" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 goto run
)

echo Python 3.10 or later could not be started. Install Python and run Start-QA.cmd again.
popd
pause
exit /b 1

:run
if /i "%~1"=="register-key" goto register
"%QA_PYTHON%" -B "%~dp0qa.py" panel --open-browser %*
set "QA_EXIT=%ERRORLEVEL%"
popd
if not "%QA_EXIT%"=="0" pause
exit /b %QA_EXIT%

:register
"%QA_PYTHON%" -B "%~dp0qa.py" register-key
set "QA_EXIT=%ERRORLEVEL%"
popd
pause
exit /b %QA_EXIT%
