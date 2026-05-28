#requires -Version 5.1
<#
  Đọc links.txt và download tất cả ảnh Facebook bằng gallery-dl.
  Bỏ qua dòng trống và dòng bắt đầu bằng '#'.
#>

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$python   = "C:\Python313\python.exe"
$linksFile = Join-Path $PSScriptRoot "links.txt"
$config    = Join-Path $PSScriptRoot "gallery-dl.conf"
$cookies   = Join-Path $PSScriptRoot "cookies.txt"

if (-not (Test-Path $linksFile)) {
    Write-Host "[X] Khong tim thay links.txt" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $cookies)) {
    Write-Host "[!] Chua co cookies.txt - Facebook se chan request." -ForegroundColor Yellow
    Write-Host "    Xem huong dan trong README.md de export cookies." -ForegroundColor Yellow
}

$links = Get-Content $linksFile |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -and -not $_.StartsWith('#') }

if ($links.Count -eq 0) {
    Write-Host "[!] links.txt rong." -ForegroundColor Yellow
    exit 0
}

Write-Host "==> Tim thay $($links.Count) link. Bat dau download..." -ForegroundColor Cyan

$ok = 0; $fail = 0
foreach ($url in $links) {
    Write-Host ""
    Write-Host "--> $url" -ForegroundColor Cyan
    & $python -m gallery_dl --config $config $url
    if ($LASTEXITCODE -eq 0) { $ok++ } else { $fail++ }
}

Write-Host ""
Write-Host "==> Xong: $ok thanh cong, $fail loi." -ForegroundColor Green
Write-Host "    File da luu trong .\downloads\" -ForegroundColor Green
