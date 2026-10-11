@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 goto use_py

if exist "%USERPROFILE%\.local\bin\uv.exe" goto use_uv

where python >nul 2>nul
if errorlevel 1 goto no_python
start "Daybook" "http://127.0.0.1:8765"
python server.py
goto end

:use_py
start "Daybook" "http://127.0.0.1:8765"
py server.py
goto end

:use_uv
start "Daybook" "http://127.0.0.1:8765"
"%USERPROFILE%\.local\bin\uv.exe" run --python 3.12 --no-project server.py
goto end

:no_python
echo Python 3.10 or newer is required. Install Python or uv, then run this file again.
pause

:end
endlocal