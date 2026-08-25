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
echo ==^> Build (onedir vi co PyTorch/Whisper, no console)...
echo     Luu y: neu dang cai torch ban CUDA, thu muc dist se nang ~4GB thay vi ~700MB.
"%PYTHON%" -m PyInstaller ^
    --noconfirm ^
    --onedir ^
    --windowed ^
    --name "FB_Downloader" ^
    --add-data "gallery-dl.conf;." ^
    --collect-all gallery_dl ^
    --collect-all yt_dlp ^
    --collect-all yt_dlp_ejs ^
    --collect-all whisper ^
    --collect-all tiktoken ^
    --collect-all tiktoken_ext ^
    --collect-all torch ^
    --collect-all numba ^
    --hidden-import gallery_dl ^
    --hidden-import yt_dlp ^
    --hidden-import yt_dlp_ejs ^
    --hidden-import whisper ^
    src\fb_downloader.py

if errorlevel 1 (
    echo [X] PyInstaller loi.
    pause
    exit /b 1
)

echo.
echo ==^> Xong! Thu muc: dist\FB_Downloader\  (chay FB_Downloader.exe ben trong)
echo.
dir /b dist\FB_Downloader
echo.
pause
