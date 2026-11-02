# Encoding health check: find source files written as UTF-16 / with BOM / wrong line endings.
#
# Background: on 2026-09-30 a concurrent session used PowerShell Set-Content / Out-File /
# `>` redirection to write test/SildurLite/shaders/composite2.fsh, producing UTF-16LE
# (19332 bytes, 9665 NUL). git then classified the file as binary: `git diff` stopped
# working entirely, the change could not be reviewed or rolled back per-line, and the
# shader loader (which reads UTF-8) would have gotten garbage from the `#version` header.
#
# This script is deliberately ASCII-only. PowerShell 5.1 reads a BOM-less .ps1 using the
# system ANSI codepage, so any non-ASCII character in this file would corrupt the parse on
# a non-UTF-8 machine. Keep it that way.
#
# Usage:  powershell -File check-encoding.ps1 [-Root <path>] [-Fix]
#   -Fix  converts pure UTF-16LE files back to UTF-8/LF; leaves other cases alone.
#
# Exit code: 0 = clean, 1 = problems found.

param(
  # Default = repo root, derived from this script's own location
  # (<repo>\tools\check-encoding.ps1 -> repo root is $PSScriptRoot's parent).
  # It used to be hardcoded "C:\poject\litematica-preview", which only resolved
  # because a junction C:\poject -> D:\project was created out-of-band; a move,
  # a reinstall or a clean clone broke it with a bare "path does not exist".
  [string]$Root = (Split-Path -Parent $PSScriptRoot),
  [switch]$Fix
)

$ErrorActionPreference = "Stop"

# extension -> expected line ending
$expect = @{
  ".fsh"       = "LF"
  ".vsh"       = "LF"
  ".glsl"      = "LF"
  ".cpp"       = "LF"
  ".hpp"       = "LF"
  ".h"         = "LF"
  ".c"         = "LF"
  ".txt"       = "LF"
  ".md"         = "LF"
  ".py"        = "LF"
  ".eko"       = "LF"
  ".expected"  = "LF"
  ".ps1"       = "CRLF"
}

# Skip: git internals, build output, and vendored third-party trees.
# `extern/` holds upstream sources (zlib etc.) whose historical encodings are not
# ours to enforce -- e.g. extern/zlib/CMakeLists.txt is not valid UTF-8 and never was.
$skip = '\\\.git\\|\\build\\|\\CMakeFiles\\|\\out\\|\\node_modules\\|\\backups\\|\\extern\\'

$issues = New-Object System.Collections.ArrayList

function Add-Issue($file, $problem, $detail, $fatal) {
  $null = $issues.Add([pscustomobject]@{
    File = $file; Problem = $problem; Detail = $detail; Fatal = $fatal
  })
}

$files = Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue

foreach ($f in $files) {
  if ($f.FullName -match $skip) { continue }

  $ext = $f.Extension.ToLower()
  if (-not $expect.ContainsKey($ext)) { continue }
  if ($f.Length -eq 0) { continue }
  if ($f.Length -gt 4MB) { continue }   # skip huge files, keep the scan fast

  $b = [IO.File]::ReadAllBytes($f.FullName)
  $rel = $f.FullName.Substring($Root.Length)
  $n = $b.Length

  # --- UTF-16 BOM ---
  if ($n -ge 2 -and $b[0] -eq 0xFF -and $b[1] -eq 0xFE) {
    Add-Issue $rel "UTF-16LE BOM" "$n bytes" $true
    if ($Fix) {
      try {
        $txt  = [Text.Encoding]::Unicode.GetString($b, 2, $n - 2)
        $out  = $txt -replace "`r`n", "`n"
        [IO.File]::WriteAllText($f.FullName, $out, (New-Object Text.UTF8Encoding($false)))
        $issues[$issues.Count-1].Detail += " -> FIXED to UTF-8/LF"
        $issues[$issues.Count-1].Fatal = $false
      } catch {
        $issues[$issues.Count-1].Detail += " -> FIX FAILED"
      }
    }
    continue
  }
  if ($n -ge 2 -and $b[0] -eq 0xFE -and $b[1] -eq 0xFF) {
    Add-Issue $rel "UTF-16BE BOM" "$n bytes" $true
    continue
  }

  # --- UTF-8 BOM (PS 5.1 -Encoding utf8 default output) ---
  if ($n -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) {
    Add-Issue $rel "UTF-8 BOM" "PS 5.1 default artifact" $false
    if ($Fix) {
      [IO.File]::WriteAllText(
        $f.FullName,
        [Text.Encoding]::UTF8.GetString($b, 3, $n - 3),
        (New-Object Text.UTF8Encoding($false))
      )
      $issues[$issues.Count-1].Detail += " -> FIXED (BOM stripped)"
    }
    continue
  }

  # --- invalid UTF-8 ---
  $valid = $true
  try { $null = (New-Object Text.UTF8Encoding($false, $true)).GetString($b) }
  catch { $valid = $false }
  if (-not $valid) {
    Add-Issue $rel "invalid UTF-8" "not decodable as UTF-8" $true
    continue
  }

  # --- stray NUL (not the UTF-16 case) ---
  $nul = 0
  foreach ($x in $b) { if ($x -eq 0) { $nul++ } }
  if ($nul -gt 0) {
    Add-Issue $rel "$nul NUL bytes" "suspect write path" $true
    continue
  }

  # --- line endings ---
  $crlf = 0
  for ($i = 0; $i -lt $n - 1; $i++) { if ($b[$i] -eq 13 -and $b[$i+1] -eq 10) { $crlf++ } }
  $lf = 0
  foreach ($x in $b) { if ($x -eq 10) { $lf++ } }

  if ($expect[$ext] -eq "LF" -and $crlf -gt 0) {
    Add-Issue $rel "$crlf CRLF (want LF)" "git diff will show whole file" $false
  }
  if ($expect[$ext] -eq "CRLF" -and $lf -gt 0 -and $crlf -eq 0) {
    Add-Issue $rel "LF only (want CRLF)" "advisory" $false
  }
}

if ($issues.Count -eq 0) {
  "OK  no encoding issues under $Root"
  exit 0
}

$fat = @($issues | Where-Object { $_.Fatal }).Count
$adv = $issues.Count - $fat

if ($fat -eq 0) {
  "OK  no fatal encoding issues under $Root   (advisory: $adv)"
  if ($adv -gt 0) {
    ""
    "Advisory (non-blocking), grouped by kind:"
    $issues | Group-Object { if ($_.Problem -match "CRLF|LF only") { "line endings" } else { $_.Problem } } |
      Sort-Object Count -Descending |
      ForEach-Object { "  {0,5}  {1}" -f $_.Count, $_.Name }
    ""
    "Non line-ending advisories worth a look:"
    $issues | Where-Object { $_.Problem -notmatch "CRLF|LF only" } |
      ForEach-Object { "  {0}  -- {1}" -f $_.File, $_.Problem }
  }
  exit 0
}

"=== $($issues.Count) encoding issue(s) under $Root ==="
$issues | Where-Object { $_.Fatal } | Format-Table -AutoSize -Wrap
"Fatal: $fat    Advisory: $adv"
exit 1
