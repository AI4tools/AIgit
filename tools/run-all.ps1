<#
.SYNOPSIS
  aigit full gate: encoding check + 12 self-running suites + concurrency suite
  (+ optional mutation baseline), one command, one red/green answer.

.DESCRIPTION
  Why this exists: the criteria are spread over 13 entry points; remembering all
  of them is not a control, it is a habit. This script answers exactly one
  question: "does the CURRENT WORKING TREE still satisfy every existing
  criterion?"

  The mutation baseline (-WithMutation) is NOT run in the working tree.
  mutation_runner's `--base-ref HEAD` means `git archive HEAD`; uncommitted
  changes are not in it, so running it as-is would silently test the OLD code.
  This script therefore copies the working tree into a throwaway snapshot repo,
  commits it locally, and runs the runner there.

  ASCII-only on purpose: PowerShell 5.1 reads a BOM-less .ps1 using the system
  ANSI codepage, so any non-ASCII character here would corrupt the parse on a
  non-UTF-8 machine (same rule as tools/check-encoding.ps1).

.PARAMETER Fast
  Encoding check + 12 suites only (~6 min). Skips concurrency and mutation.

.PARAMETER WithMutation
  Also run the full mutation baseline (~21 min, needs >= 2 GB free disk).

.PARAMETER Python
  Interpreter command, default "python".

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\run-all.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\run-all.ps1 -WithMutation
#>
param(
    [switch]$Fast,
    [switch]$WithMutation,
    [string]$Python = "python"
)

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$root = Split-Path -Parent $PSScriptRoot
$pyroot = Join-Path $root "python-ref"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logdir = Join-Path $env:TEMP ("aigit-gate-" + $stamp)
New-Item -ItemType Directory -Path $logdir -Force | Out-Null

$SUITES = @(
    "test_aigit.py",
    "test_aigit_contract.py",
    "test_conflicts.py",
    "test_history_ops.py",
    "test_f06_guard.py",
    "test_index_fastpath.py",
    "test_refstore.py",
    "test_write_inputs.py",
    "test_object_read.py",
    "test_history_fast.py",
    "test_vs_git.py",
    "test_write_vs_git.py",
    "test_matrix_vs_git.py",
    "test_api_contract.py",
    "test_aigit_extra.py",
    "test_aigit_web.py",
    "test_aigit_invariants.py",
    "test_aigit_net.py",
    "test_aigit_gate.py",
    "test_blame_guard_signatures.py"
)

$script:results = @()

function Add-Result([string]$name, [int]$rc, [double]$sec, [string]$tail) {
    $script:results += [pscustomobject]@{ Step = $name; Rc = $rc; Sec = $sec; Tail = $tail }
}

function Summarize-Log([string]$log) {
    if (-not (Test-Path $log)) { return "" }
    $lines = Get-Content $log -Encoding UTF8 -ErrorAction SilentlyContinue
    if (-not $lines) { return "" }
    # Priority 1: explicit machine summary shapes (ASCII only -- this script
    # must stay ASCII because PS 5.1 reads BOM-less scripts as ANSI).
    $pats = @(
        "PASS \d+.*FAIL \d+",
        "PASS *\/ *\d+ *FAIL",
        "passed \d+ of \d+",
        "killed \d+|KILLED",
        "no encoding issues|encoding issues",
        "Ran \d+ tests"
    )
    foreach ($p in $pats) {
        $hit = $lines | Select-String -Pattern $p | Select-Object -Last 1
        if ($hit) { return (($hit.Line -replace "\s+", " ").Trim()) }
    }
    # Fallback: last non-empty line that is NOT a filesystem path (cleanup
    # messages such as "cleanup done C:\...\aigit-history-test-123 deleted"
    # would otherwise win) and DOES contain a digit.
    $nonEmpty = @($lines | Where-Object { $_.Trim() -ne "" })
    for ($i = $nonEmpty.Count - 1; $i -ge 0; $i--) {
        $ln = $nonEmpty[$i].Trim()
        if ($ln -match "[A-Za-z]:\\") { continue }
        if ($ln -notmatch "\d") { continue }
        return ($ln -replace "\s+", " ")
    }
    if ($nonEmpty.Count -gt 0) { return (($nonEmpty[-1] -replace "\s+", " ").Trim()) }
    return ""
}

function Invoke-Step([string]$name, [string]$exe, [string[]]$argv, [string]$workdir) {
    $log = Join-Path $logdir ($name + ".log")
    $t0 = Get-Date
    Push-Location $workdir
    try {
        & $exe @argv *> $log
        $rc = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    $sec = ((Get-Date) - $t0).TotalSeconds
    $tail = Summarize-Log $log
    Add-Result $name $rc $sec $tail
    if ($rc -eq 0) { $mark = "ok  " } else { $mark = "FAIL" }
    Write-Host ("  {0}  {1,-28} {2,7:N1}s  {3}" -f $mark, $name, $sec, $tail)
}

$mode = "Full (with concurrency)"
if ($Fast) { $mode = "Fast (no concurrency, no mutation)" }
if ($WithMutation) { $mode = "Full + mutation baseline" }

Write-Host ""
Write-Host ("  aigit gate  " + $stamp)
Write-Host ("  root   : " + $root)
Write-Host ("  logs   : " + $logdir)
Write-Host ("  mode   : " + $mode)
Write-Host ""

# 1) encoding gate
$gate = Join-Path $root "tools\check-encoding.ps1"
if (Test-Path $gate) {
    Invoke-Step "encoding-gate" "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $gate, $root) $root
} else {
    Add-Result "encoding-gate" 1 0 ("missing: " + $gate)
    Write-Host "  FAIL  encoding-gate               script not found"
}

# 2) 12 self-running suites
foreach ($s in $SUITES) {
    if (-not (Test-Path (Join-Path $pyroot $s))) {
        Add-Result $s 1 0 "file not found"
        Write-Host ("  FAIL  {0,-28} file not found" -f $s)
        continue
    }
    Invoke-Step $s $Python @("-X", "utf8", $s) $pyroot
}

# 3) concurrency suite
if (-not $Fast) {
    Invoke-Step "test_aigit_concurrency.py" $Python @("-X", "utf8", "test_aigit_concurrency.py") $pyroot
}

# 3b) performance budget (spawn is the hard criterion, wall clock is a band)
if (-not $Fast) {
    $budget = Join-Path $pyroot "tools\perf-budget.py"
    if (Test-Path $budget) {
        Invoke-Step "perf-budget" $Python @("-X", "utf8", $budget,
            "--json", (Join-Path $logdir "perf-budget.json")) $root
    } else {
        Add-Result "perf-budget" 1 0 "missing: python-ref/tools/perf-budget.py"
        Write-Host "  FAIL  perf-budget                 script not found"
    }
}

# 4) mutation baseline, inside a throwaway snapshot repo
if ($WithMutation) {
    $snap = Join-Path $env:TEMP ("aigit-gate-snap-" + $stamp)
    $snapPkg = Join-Path $snap "aigit"
    Write-Host ("  preparing mutation snapshot: " + $snapPkg)
    New-Item -ItemType Directory -Path $snapPkg -Force | Out-Null
    & robocopy $root $snapPkg /E /XD .git __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
    Push-Location $snapPkg
    try {
        & git init -q
        & git config user.name gate
        & git config user.email gate@localhost
        & git config core.autocrlf false
        & git config core.eol lf
        & git add -A 2>$null
        & git commit -q -m ("gate snapshot " + $stamp) 2>$null
        $sha = (& git rev-parse --short HEAD).Trim()
    } finally {
        Pop-Location
    }
    $outJson = Join-Path $logdir "baseline.json"
    $mutLogs = Join-Path $logdir "mutlogs"
    Invoke-Step "mutation-baseline" $Python @("-X", "utf8",
        (Join-Path $snapPkg "mutation_runner.py"), "--all", "--workers", "4",
        "--base-ref", $sha, "--out", $outJson, "--log-dir", $mutLogs) $snapPkg
    Remove-Item $snap -Recurse -Force -ErrorAction SilentlyContinue
}

# 5) summary
Write-Host ""
Write-Host ("  " + ("-" * 78))
$failed = @($script:results | Where-Object { $_.Rc -ne 0 })
$total = ($script:results | Measure-Object -Property Sec -Sum).Sum
Write-Host ("  steps: {0}   failed: {1}   elapsed: {2:N0}s" -f $script:results.Count, $failed.Count, $total)
if ($failed.Count -gt 0) {
    foreach ($f in $failed) {
        Write-Host ("    FAIL {0,-26} rc={1}  {2}" -f $f.Step, $f.Rc, $f.Tail)
    }
    Write-Host ("  logs: " + $logdir)
    exit 1
}
Write-Host "  ALL PASS"
Write-Host ("  logs: " + $logdir)
exit 0
