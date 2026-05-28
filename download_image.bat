@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "PYTHON=C:\Users\ADMIN\AppData\Local\Programs\Python\Python313\python.exe"
set "LINKS=%~dp0links.txt"
set "CONFIG=%~dp0gallery-dl.conf"
set "COOKIES=%~dp0cookies.txt"
set "OUTDIR=%~dp0downloads"

if not exist "%PYTHON%" (
    echo Khong tim thay Python tai: %PYTHON%
    pause
    exit /b 1
)

if not exist "%LINKS%" (
    echo Khong tim thay links.txt trong thu muc hien tai.
    pause
    exit /b 1
)

if not exist "%CONFIG%" (
    echo Khong tim thay gallery-dl.conf trong thu muc hien tai.
    pause
    exit /b 1
)

if not exist "%OUTDIR%" mkdir "%OUTDIR%"

set "COOKIE_ARG="
if exist "%COOKIES%" (
    set "COOKIE_ARG=--cookies "%COOKIES%""
    echo [i] Dung cookies.txt
) else (
    echo [i] Khong co cookies.txt - chi tai duoc anh public.
)

echo.
echo Bat dau tai anh...
echo Luu vao: %OUTDIR%
echo.

"%PYTHON%" -m gallery_dl ^
    --config "%CONFIG%" ^
    %COOKIE_ARG% ^
    --input-file "%LINKS%"

echo.
echo Da chay xong.
pause
