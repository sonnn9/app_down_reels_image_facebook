@echo off
setlocal

cd /d "%~dp0"

set "OUTDIR=%~dp0downloads"

if not exist "%~dp0yt-dlp.exe" (
    echo Khong tim thay yt-dlp.exe trong thu muc hien tai.
    pause
    exit /b 1
)

if not exist "%~dp0reels.txt" (
    echo Khong tim thay reels.txt trong thu muc hien tai.
    pause
    exit /b 1
)

if not exist "%OUTDIR%" mkdir "%OUTDIR%"

echo.
echo Bat dau tai video...
echo Luu vao: %OUTDIR%
echo.

"%~dp0yt-dlp.exe" ^
--ignore-config ^
-a "%~dp0reels.txt" ^
-P "%OUTDIR%" ^
-o "%%(id)s.%%(ext)s" ^
--windows-filenames ^
--no-download-archive

echo.
echo Da chay xong.
pause