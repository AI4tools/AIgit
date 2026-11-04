# package.ps1 -- assemble a self-contained folder/zip: aigit.exe + web + start.bat.
# ASCII only (Windows PowerShell 5.1 reads BOM-less scripts as ANSI).
# The Chinese user-facing README lives in README-dist.txt (copied as README.txt).
param(
    [string] $Out = '',
    [switch] $NoZip
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot 'build.ps1')

$distRoot = Join-Path $PSScriptRoot 'dist'
$dist = Join-Path $distRoot 'aigit-c'
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
New-Item -ItemType Directory -Path $dist -Force | Out-Null

Copy-Item (Join-Path $PSScriptRoot 'build\aigit.exe') (Join-Path $dist 'aigit.exe')
Copy-Item (Join-Path $root 'web') (Join-Path $dist 'web') -Recurse
Copy-Item (Join-Path $PSScriptRoot 'README.md') (Join-Path $dist 'README.md')
Copy-Item (Join-Path $PSScriptRoot 'README-dist.txt') (Join-Path $dist 'README.txt')
if (Test-Path (Join-Path $root 'LICENSE')) {
    Copy-Item (Join-Path $root 'LICENSE') (Join-Path $dist 'LICENSE')
}

$start = @'
@echo off
setlocal
cd /d "%~dp0"
rem usage: start.bat [repo] [port]
set "PORT=%~2"
if "%PORT%"=="" set "PORT=8777"
set "REPO=%~1"
if "%REPO%"=="" set "REPO=%~dp0workspace"
echo aigit 1.0.0
echo   repo: %REPO%
echo   port: %PORT%  (auto-increments if busy)
echo   log : %~dp0aigit.log
echo   a browser will open; close this window to stop.
echo.
aigit.exe --root "%REPO%" --port %PORT% --web "%~dp0web" --open >> "%~dp0aigit.log" 2>&1
if errorlevel 1 (
  echo aigit failed to start, see aigit.log
  pause
)
'@
[IO.File]::WriteAllText((Join-Path $dist 'start.bat'), ($start -replace "`r?`n", "`r`n"), (New-Object Text.ASCIIEncoding))

if (-not $NoZip) {
    if (-not $Out) { $Out = Join-Path $distRoot 'aigit-c.zip' }
    if (Test-Path $Out) { Remove-Item $Out -Force }
    Compress-Archive -Path (Join-Path $dist '*') -DestinationPath $Out -Force
    Write-Host ("zipped: {0} ({1:N0} bytes)" -f $Out, (Get-Item $Out).Length)
} else {
    Write-Host ("folder: {0}" -f $dist)
}
