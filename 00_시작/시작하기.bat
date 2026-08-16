@echo off
chcp 65001 >nul
REM Slide Tool starter. Keep this file ASCII-only.
setlocal
set "PY="

for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%P"
if defined PY goto :run
for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%P"
if defined PY goto :run
for /f "delims=" %%P in ('python3 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%P"
if defined PY goto :run
goto :no_python

:no_python
echo.
echo [!] Python 3 was not found.
echo 1^) Install Python with winget.
echo 2^) Open the python.org download page.
set /p "CHOICE=Select 1 or 2, then press Enter: "
if "%CHOICE%"=="1" goto :install
if "%CHOICE%"=="2" goto :download
echo Invalid selection.
pause
exit /b 1

:install
where winget >nul 2>nul
if errorlevel 1 goto :download
winget install -e --id Python.Python.3.12
set "INSTALL_RC=%ERRORLEVEL%"
echo Re-run this file after installation completes.
pause
exit /b %INSTALL_RC%

:download
start "" "https://www.python.org/downloads/"
echo Check "Add python.exe to PATH" during installation.
echo Re-run this file after installation completes.
pause
exit /b 1

:run
"%PY%" "%~dp0launch.py" --auto
set "RUN_RC=%ERRORLEVEL%"
pause
exit /b %RUN_RC%
