@echo off
setlocal
set "SCRIPT=%~dp0keep-or-delete-with-photostructure.py"
if not exist "%SCRIPT%" set "SCRIPT=%USERPROFILE%\keep-or-delete-with-photostructure\keep-or-delete-with-photostructure.py"
if not exist "%SCRIPT%" (
  echo Could not find keep-or-delete-with-photostructure.py next to this file or in %%USERPROFILE%%\keep-or-delete-with-photostructure\.
  pause
  exit /b 1
)

if exist "%~dp0pythonw.exe" (
  start "" "%~dp0pythonw.exe" "%SCRIPT%"
  exit /b 0
)

where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw "%SCRIPT%"
  exit /b 0
)

where py >nul 2>&1
if %errorlevel%==0 (
  start "" py -3 "%SCRIPT%"
  exit /b 0
)

where python >nul 2>&1
if %errorlevel%==0 (
  python "%SCRIPT%"
  exit /b %errorlevel%
)

echo Could not find Python. Install Python or add pythonw.exe to PATH.
pause
exit /b 1
