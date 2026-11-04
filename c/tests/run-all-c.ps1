# run-all-c.ps1 -- one-command local gate for the C implementation.
# ASCII only (Windows PowerShell 5.1 reads BOM-less scripts as ANSI).
param(
    [switch] $Fast
)

$ErrorActionPreference = 'Stop'
$croot = Split-Path -Parent $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

Write-Host '== build + 16 conformance shards =='
& (Join-Path $croot 'build.ps1') -Test

Write-Host '== guard: repo config cannot execute =='
& python -X utf8 (Join-Path $PSScriptRoot 'guard_adv_c.py')
if ($LASTEXITCODE -ne 0) { throw 'guard_adv failed' }

Write-Host '== unicode names / ints / big ints =='
& python -X utf8 (Join-Path $PSScriptRoot 'unicode_name_c.py')
if ($LASTEXITCODE -ne 0) { throw 'unicode_name failed' }
& python -X utf8 (Join-Path $PSScriptRoot 'unicode_int_c.py')
if ($LASTEXITCODE -ne 0) { throw 'unicode_int failed' }
& python -X utf8 (Join-Path $PSScriptRoot 'bigint_c.py')
if ($LASTEXITCODE -ne 0) { throw 'bigint failed' }

Write-Host '== end-to-end scenario (real unittest, 5 nodes, 3 rollbacks) =='
& python -X utf8 (Join-Path $PSScriptRoot 'scenario_session.py')
if ($LASTEXITCODE -ne 0) { throw 'scenario failed' }

Write-Host '== launch hardening: Host/Origin gate, port fallback, --version =='
& python -X utf8 (Join-Path $PSScriptRoot 'launch_guard_c.py')
if ($LASTEXITCODE -ne 0) { throw 'launch_guard failed' }

Write-Host '== batch diff endpoint (C superset) =='
& python -X utf8 (Join-Path $PSScriptRoot 'batch_diff_c.py')
if ($LASTEXITCODE -ne 0) { throw 'batch_diff failed' }

Write-Host '== end-to-end scenario: batch stage/discard/reset (300 and 1000 files) =='
& python -X utf8 (Join-Path $PSScriptRoot 'scenario_batch.py')
if ($LASTEXITCODE -ne 0) { throw 'scenario_batch failed' }

Write-Host '== end-to-end scenario: diff review reset (unstage/discard/reset) =='
& python -X utf8 (Join-Path $PSScriptRoot 'scenario_reset.py')
if ($LASTEXITCODE -ne 0) { throw 'scenario_reset failed' }

Write-Host '== end-to-end scenario: merge conflict + abort/choose =='
& python -X utf8 (Join-Path $PSScriptRoot 'scenario_conflict.py')
if ($LASTEXITCODE -ne 0) { throw 'scenario_conflict failed' }

if ($Fast) {
    Write-Host '== fast mode: skipped concurrency/soak/fuzz/mutation =='
    exit 0
}

Write-Host '== concurrency =='
& python -X utf8 (Join-Path $PSScriptRoot 'concurrency_c.py') 12 25
if ($LASTEXITCODE -ne 0) { throw 'concurrency failed' }

Write-Host '== soak =='
& python -X utf8 (Join-Path $PSScriptRoot 'soak_c.py') 8 120
if ($LASTEXITCODE -ne 0) { throw 'soak failed' }

Write-Host '== malformed HTTP =='
& python -X utf8 (Join-Path $PSScriptRoot 'fuzz_http_c.py') 200
if ($LASTEXITCODE -ne 0) { throw 'fuzz_http failed' }

Write-Host '== mutation baseline =='
& python -X utf8 (Join-Path $PSScriptRoot 'mutation_c.py')
if ($LASTEXITCODE -ne 0) { throw 'mutation baseline failed' }

Write-Host '== all C gates passed =='
