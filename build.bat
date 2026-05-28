@echo off
setlocal

cd /d "%~dp0"

set "PYTHON=C:\Python313\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo ==^> Cai dat / cap nhat dependency...
"%PYTHON%" -m pip install -U -r requirements.txt
if errorlevel 1 (
    echo [X] pip install loi.
    pause
    exit /b 1
)

echo.
echo ==^> Xoa build cu...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
if exist FB_Downloader.spec del /q FB_Downloader.spec

echo.
echo ==^> Build EXE (single file, no console)...
"%PYTHON%" -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "FB_Downloader" ^
    --add-data "gallery-dl.conf;." ^
    --collect-all gallery_dl ^
    --collect-all yt_dlp ^
    --hidden-import gallery_dl ^
    --hidden-import yt_dlp ^
    src\fb_downloader.py

if errorlevel 1 (
    echo [X] PyInstaller loi.
    pause
    exit /b 1
)

echo.
echo ==^> Xong! File: dist\FB_Downloader.exe
echo.
dir /b dist
echo.
pause
