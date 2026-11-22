@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer and try again.
    pause
    exit /b 1
)

echo [1/3] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [2/3] Building BatchMediaLite.exe...
set "EXTRA_ARGS="
if exist "assets\icon.ico" set "EXTRA_ARGS=--icon assets/icon.ico --add-data assets/icon.ico;assets"

python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name "BatchMediaLite" ^
    --collect-all customtkinter ^
    --collect-all tkinterdnd2 ^
    %EXTRA_ARGS% ^
    app.py
if errorlevel 1 goto :error

echo.
echo [3/3] Build complete.
echo The portable app is here: dist\BatchMediaLite.exe
echo.
pause
exit /b 0

:error
echo.
echo Build failed. Review the messages above.
echo.
pause
exit /b 1
