# build.ps1 -- one-command build for the C implementation.
# ASCII only (Windows PowerShell 5.1 reads BOM-less scripts as ANSI).
param(
    [switch] $Clean,
    [switch] $Test
)

$ErrorActionPreference = 'Stop'

# Toolchain discovery: AIGIT_MINGW -> common install dirs -> mingw32-make/make on PATH.
# Keeps CI (msys2 at C:\msys64) and other machines working without editing scripts.
$make = $null
$mingw = $env:AIGIT_MINGW
if (-not $mingw) {
    $cands = @('C:\Environment\cpp\mingw64', 'C:\msys64\mingw64')
    if ($env:ProgramFiles) { $cands += (Join-Path $env:ProgramFiles 'mingw64') }
    foreach ($cand in $cands) {
        if (Test-Path (Join-Path $cand 'bin\mingw32-make.exe')) { $mingw = $cand; break }
    }
}
if ($mingw) {
    $bin = Join-Path $mingw 'bin'
    $make = Join-Path $bin 'mingw32-make.exe'
    if (-not (Test-Path $make)) { $make = Join-Path $bin 'make.exe' }
    $env:PATH = $bin + ';' + $env:PATH
}
if (-not $make -or -not (Test-Path $make)) {
    $cmd = Get-Command mingw32-make -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command make -ErrorAction SilentlyContinue }
    if (-not $cmd) {
        throw 'mingw32-make not found. Install MinGW-w64 and set AIGIT_MINGW, or put its bin on PATH.'
    }
    $make = $cmd.Source
}

if ($Clean) {
    & $make -C $PSScriptRoot clean
    if ($LASTEXITCODE -ne 0) { throw 'clean failed' }
}

& $make -C $PSScriptRoot -j4
if ($LASTEXITCODE -ne 0) { throw 'build failed' }

$exe = Join-Path $PSScriptRoot 'build\aigit.exe'
if (-not (Test-Path $exe)) { throw "build did not produce $exe" }
Write-Host ("built: {0} ({1:N0} bytes)" -f $exe, (Get-Item $exe).Length)

if ($Test) {
    & python -X utf8 (Join-Path $PSScriptRoot 'tests\run_conformance.py')
    if ($LASTEXITCODE -ne 0) { throw 'conformance failed' }
}
