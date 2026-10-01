<#
.SYNOPSIS
    aigit launcher (PowerShell edition).

.DESCRIPTION
    Starts server.py, the local git GUI. This script does the boring work so
    you do not have to: find Python 3, find git, sanity-check the arguments,
    refuse to point a destructive tool at a repo with uncommitted work, and
    refuse to start on a port somebody else already owns.

    It NEVER opens a browser and NEVER spawns a visible window. It prints a
    URL and you decide. That is deliberate: several development lines run on
    this machine at once, and a popup interrupts people.

.PARAMETER Repo
    Repository directory to open. Default: <TEMP>\aigit-sandbox
    If the directory does not exist, server.py creates and initialises it.

.PARAMETER Port
    TCP port to listen on. Default: 8777

.PARAMETER Sandbox
    Delete -Repo's directory entirely and rebuild it as a throwaway repo.
    Refuses to run if that directory is a git repo with a remote configured
    (that is a real repo, not a sandbox) unless -Force is given.

.PARAMETER Force
    Skip the two safety interlocks:
      * a repo with uncommitted changes to tracked files
      * -Sandbox aimed at a directory whose repo has a remote

.PARAMETER Python
    Explicit Python 3 executable. Use this for an embeddable/portable Python,
    or to pick a specific install when several exist.

.PARAMETER ServerArgs
    Extra arguments handed straight to server.py.

.PARAMETER Pause
    Wait for Enter before exiting. Only useful when this script is started
    from a window that would otherwise close on its own.

.PARAMETER Help
    Print usage and exit.

.EXAMPLE
    .\aigit.ps1
    Open <TEMP>\aigit-sandbox on port 8777.

.EXAMPLE
    .\aigit.ps1 -Sandbox
    Wipe and rebuild the sandbox repo.

.EXAMPLE
    .\aigit.ps1 -Repo C:\work\myrepo -Port 9000
    Open an existing repo on a different port.

.NOTES
    Encoding: this file is deliberately ASCII-only, saved as UTF-8 WITHOUT BOM
    with CRLF line endings.

    Why ASCII-only, when a Chinese user is the main audience:
      * PowerShell 5.1 reads a BOM-less .ps1 using the system ANSI code page
        (936/GBK on a Chinese install). Chinese stored as UTF-8 gets decoded
        as GBK there, and some byte pairs turn into quote characters, so the
        parser fails at a line that has nothing to do with the real problem.
        This project has been bitten by that twice already.
      * tools\check-encoding.ps1 treats a non-UTF-8 .ps1 as a FATAL encoding
        issue and exits 1 for the whole repository, so a GBK .ps1 would turn
        the repo-wide gate red.

    aigit.bat is the Chinese-message entry point: cmd.exe is already cp936, so
    a GBK .bat renders correctly and check-encoding.ps1 skips .bat entirely.
    Use that one for the guided error messages; use this one for scripting.
#>

#requires -Version 5.1
[CmdletBinding()]
param(
    [string] $Repo,
    [string] $Port = '8777',
    [switch] $Sandbox,
    [switch] $Force,
    [string] $Python,
    [string[]] $ServerArgs = @(),
    [switch] $Pause,
    [switch] $Help
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:HERE = Split-Path -Parent $MyInvocation.MyCommand.Definition

# --------------------------------------------------------------------------- utils

function Stop-Aigit {
    param([string] $Title, [string[]] $Lines, [int] $Code = 1)
    Write-Host ''
    Write-Host ('=' * 64) -ForegroundColor DarkGray
    Write-Host ("  [startup failed] {0}" -f $Title) -ForegroundColor Red
    Write-Host ('=' * 64) -ForegroundColor DarkGray
    foreach ($l in $Lines) { Write-Host ("  {0}" -f $l) }
    Write-Host ''
    Write-Host '  This is not a crash. A pre-flight check did not pass.'
    Write-Host '  Fix the item above and run this again.' -ForegroundColor DarkGray
    Write-Host ''
    if ($Pause) { $null = Read-Host '  Press Enter to close' }
    exit $Code
}

function Say-Warn { param([string] $Text) Write-Host ("[warn] {0}" -f $Text) -ForegroundColor Yellow }
function Say-Note { param([string] $Text) Write-Host ("  {0}" -f $Text) }

function Get-App {
    param([string] $Name)
    $c = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
         Select-Object -First 1
    if ($null -eq $c) { return $null }
    return $c.Source
}

function Test-Python3 {
    param([string[]] $Cmd)
    if ($Cmd.Count -eq 0) { return $false }
    $exe = $Cmd[0]
    $pre = @()
    if ($Cmd.Count -gt 1) { $pre = @($Cmd[1..($Cmd.Count - 1)]) }
    if ($pre.Count -gt 0) {
        if ($null -eq (Get-App $exe)) { return $false }
    }
    try {
        & $exe @pre -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Test-PortInUse {
    param([int] $PortNumber)
    $l = $null
    try {
        $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $PortNumber)
        $l.Start()
        $l.Stop()
        return $false
    } catch [System.Net.Sockets.SocketException] {
        return $true
    } finally {
        if ($null -ne $l) { try { $l.Stop() } catch { } }
    }
}

function Get-GitLines {
    param([string] $Dir, [string[]] $GitArgs)
    $out = & git -C $Dir @GitArgs 2>$null
    # '$_ -and' matters: a failing git yields $null, and $null -ne '' is true,
    # so without it an empty result would be counted as one changed file.
    #
    # Callers must still wrap this in @(). PowerShell unrolls a one-element
    # array on return, so a repo with exactly one changed file would hand back
    # a bare [string] and .Count would then throw under Set-StrictMode.
    return @($out | Where-Object { $_ -and $_.Trim() -ne '' })
}

# --------------------------------------------------------------------------- usage

if ($Help) {
    Write-Host 'aigit - local git GUI launcher (PowerShell)'
    Write-Host ''
    Write-Host '  .\aigit.ps1 [-Repo <path>] [-Port <port>] [-Sandbox] [-Force]'
    Write-Host '               [-Python <exe>] [-ServerArgs <string[]>] [-Pause] [-Help]'
    Write-Host ''
    Write-Host '  -Repo <path>      Repository to open. Default: <TEMP>\aigit-sandbox'
    Write-Host '  -Port <port>      Listen port, 1-65535. Default: 8777'
    Write-Host '  -Sandbox          Delete the -Repo directory and rebuild a throwaway repo.'
    Write-Host '                    Refused if that directory is a repo with a remote'
    Write-Host '                    (unless -Force). This deletion is not undoable.'
    Write-Host '  -Force            Skip the two safety interlocks described above.'
    Write-Host '  -Python <exe>     Explicit Python 3. For portable/embeddable Python.'
    Write-Host '  -ServerArgs       Extra args passed straight to server.py.'
    Write-Host '  -Pause            Wait for Enter before exiting.'
    Write-Host ''
    Write-Host '  Examples:'
    Write-Host '    .\aigit.ps1 -Sandbox'
    Write-Host '    .\aigit.ps1 -Repo C:\work\myrepo -Port 9000'
    Write-Host ''
    Write-Host '  No browser is ever opened. Copy the printed URL yourself.'
    exit 0
}

# --------------------------------------------------------------------------- arg validation
# Validated by hand rather than with [ValidateRange] etc. on purpose: a
# PowerShell parameter-binding failure prints a red multi-line error block that
# reads like a crash. These print one clear line plus the correct spelling.

if ([string]::IsNullOrWhiteSpace($Repo)) {
    $tempRoot = $env:TEMP
    if ([string]::IsNullOrWhiteSpace($tempRoot)) { $tempRoot = $env:TMP }
    if ([string]::IsNullOrWhiteSpace($tempRoot)) { $tempRoot = (Get-Location).Path }
    $Repo = Join-Path $tempRoot 'aigit-sandbox'
}
$Repo = $Repo.Trim()
if ($Repo -ne '' -and $Repo.Contains('"')) {
    Stop-Aigit -Title 'invalid -Repo path' -Lines @(
        ('The path contains a double quote: "{0}"' -f $Repo),
        'A quote in the path would break argument parsing for git and for the server.',
        'Pick a path without quotes, or use -Sandbox to get a throwaway repo.'
    )
}

$PortText = ([string]$Port).Trim()
if ($PortText -notmatch '^[0-9]+$') {
    Stop-Aigit -Title ("invalid -Port value: '{0}'" -f $PortText) -Lines @(
        'The port must be a whole number between 1 and 65535.',
        'Correct spelling:  .\aigit.ps1 -Port 9000',
        'Not: -Port:9000, and not a leading zero such as -Port 09000.'
    )
}
while ($PortText.Length -gt 1 -and $PortText.StartsWith('0')) { $PortText = $PortText.Substring(1) }
$PortNumber = [int]$PortText
if ($PortNumber -lt 1 -or $PortNumber -gt 65535) {
    Stop-Aigit -Title ("invalid -Port value: {0}" -f $PortText) -Lines @(
        'The port must be between 1 and 65535.',
        'Correct spelling:  .\aigit.ps1 -Port 9000'
    )
}

$server = Join-Path $script:HERE 'server.py'
if (-not (Test-Path -LiteralPath $server -PathType Leaf)) {
    Stop-Aigit -Title 'server.py not found' -Lines @(
        ("It should be at: {0}" -f $server),
        'Keep aigit.ps1 and server.py in the same folder.'
    )
}

# --------------------------------------------------------------------------- find Python

$pyCmd = @()
if (-not [string]::IsNullOrWhiteSpace($Python)) {
    $p = $Python.Trim()
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
        Stop-Aigit -Title ("-Python path does not exist: {0}" -f $p) -Lines @(
            'Point -Python at the python.exe of a real Python 3 install.',
            'On Windows a portable build looks like:  C:\tools\python\python.exe'
        )
    }
    $pyCmd = @($p)
    if (-not (Test-Python3 $pyCmd)) {
        Stop-Aigit -Title ("{0} is not a working Python 3" -f $p) -Lines @(
            'server.py needs Python 3.8 or newer; anything older will not run it.',
            ('Check it by hand:  & "{0}" --version' -f $p),
            'If it reports Python 2.x, point -Python at a 3.x install instead.'
        )
    }
} else {
    # Candidates are plain strings, split inside the loop. Writing them as
    # @(@('py','-3'), @('python')) would look right but PowerShell flattens
    # nested arrays in an @() literal, handing us 'py','-3','python' as three
    # separate candidates -- and then 'py -3' would silently be tested as
    # plain 'py', which is the Python 2 launcher on many machines.
    foreach ($cmdText in @('py -3', 'python', 'python3')) {
        $parts = @($cmdText -split '\s+')
        if (Test-Python3 $parts) { $pyCmd = $parts; break }
    }
    if ($pyCmd.Count -eq 0) {
        $haveLauncher = ($null -ne (Get-App 'py'))
        $haveSomething = ($null -ne (Get-App 'python'))
        $why = @()
        if ($haveLauncher) {
            $why += 'The "py" launcher exists but no Python 3 is registered under it.'
            $why += 'Most often that means only Python 2 is installed.'
        } else {
            $why += 'There is no "py" launcher, and no "python" on PATH either.'
        }
        if ($haveSomething) {
            $why += 'A "python" does exist on PATH, but it is not Python 3 (or it does not run).'
        }
        Stop-Aigit -Title 'no usable Python 3 found' -Lines @(
            $why + @(
                '',
                'Do this, three steps:',
                '  1) Open https://www.python.org/downloads/windows/',
                '  2) Download the "Windows installer (64-bit)" for 3.11 or newer.',
                '     On the FIRST page of the wizard, tick:',
                '         [x] Add python.exe to PATH',
                '     That box is UNCHECKED by default. If you skip it, this launcher',
                '     will never find python, which is the single most common',
                '     cause of this message.',
                '  3) Finish the wizard, then open a NEW terminal window (the',
                '     environment variable does not refresh in the old one) and run',
                '     this script again.',
                '',
                'To confirm:  python --version   should print Python 3.x.x',
                '',
                'Behind a corporate proxy? Ask IT for the embeddable package, then',
                'point this script at it:',
                '  .\aigit.ps1 -Python C:\tools\python\python.exe'
            )
        )
    }
}

# --------------------------------------------------------------------------- find git

if ($null -eq (Get-App 'git')) {
    Stop-Aigit -Title 'git command not found' -Lines @(
        'aigit runs real git commands, so it cannot work without git.',
        'Install Git for Windows from https://git-scm.com/download/win',
        '(Next, Next, finish). Let it update PATH.',
        'Then open a NEW terminal window and run this again.'
    )
}

# --------------------------------------------------------------------------- guards

if ($Sandbox) {
    if (Test-Path -LiteralPath (Join-Path $Repo '.git')) {
        $remotes = @(Get-GitLines -Dir $Repo -GitArgs @('remote'))
        if ($remotes.Count -gt 0) {
            if ($Force) {
                Say-Warn ("-Force given: '{0}' will be deleted anyway, remote config included." -f $Repo)
            } else {
                Stop-Aigit -Title ('-Sandbox would delete "{0}", which has a git remote' -f $Repo) -Lines @(
                    ("Configured remote(s): {0}" -f ($remotes -join ', ')),
                    'A directory with a remote is a real repository, not a throwaway',
                    'sandbox, so this is refused.',
                    'Two ways forward:',
                    '  1) Use a different path as the sandbox:',
                    '       .\aigit.ps1 -Sandbox -Repo $env:TEMP\aigit-demo',
                    '  2) You really mean it as a one-off sandbox (the remote config',
                    '     goes away with it), add -Force:',
                    '       .\aigit.ps1 -Sandbox -Repo <path> -Force'
                )
            }
        }
    }
    if (-not $Force) {
        Say-Note ('-Sandbox will delete "{0}" first, then build a throwaway repo there.' -f $Repo)
        Say-Note 'Anything in that directory is gone for good.'
    }
} elseif (Test-Path -LiteralPath (Join-Path $Repo '.git')) {
    # Tracked files that are modified/staged/conflicted. -uno leaves untracked
    # files out, so a build/ directory does not trip the interlock.
    $dirty = @(Get-GitLines -Dir $Repo -GitArgs @('status', '--porcelain', '-uno'))
    if ($dirty.Count -gt 0) {
        if ($Force) {
            Say-Warn ("-Force given: ignoring {0} uncommitted change(s) to tracked files." -f $dirty.Count)
        } else {
            Stop-Aigit -Title ("{0} uncommitted change(s) to tracked files in {1}" -f $dirty.Count, $Repo) -Lines @(
                'The Discard button in the aigit UI really does modify your working',
                'tree. Uncommitted work that you discard is not recoverable.',
                'Three ways forward:',
                '  1) Deal with it yourself first: git status, then git stash or git commit.',
                '  2) Use a clean throwaway repo:  .\aigit.ps1 -Sandbox',
                '  3) You are certain the changes do not matter, override with:',
                '       .\aigit.ps1 -Repo <path> -Force',
                '',
                'This is a hard rule in the aigit docs: do not run this tool against',
                'a real repository that has uncommitted work you care about.'
            )
        }
    }
    # Untracked files are not blocked, only reported: the Discard button deletes
    # them outright, so the user deserves to know before clicking it.
    $all = @(Get-GitLines -Dir $Repo -GitArgs @('status', '--porcelain'))
    if ($all.Count -gt $dirty.Count) {
        Say-Warn ("{0} untracked file(s) in this repo." -f ($all.Count - $dirty.Count))
        Say-Note 'Discard DELETES an untracked file; it does not restore a previous version.'
        Say-Note 'Confirm those files do not matter before you click it.'
    }
} elseif (Test-Path -LiteralPath $Repo) {
    Stop-Aigit -Title ("{0} is not a git repository" -f $Repo) -Lines @(
        'That directory exists but has no .git inside it.',
        'Either run "git init" in it, or point -Repo at a directory that is a repo.',
        'To just try the tool out, run  .\aigit.ps1  with no arguments: it uses the',
        'throwaway sandbox in your TEMP folder.'
    )
}

# --------------------------------------------------------------------------- port

if (Test-PortInUse $PortNumber) {
    Stop-Aigit -Title ("port {0} is already in use" -f $PortNumber) -Lines @(
        'aigit cannot start: only one process can listen on a given port.',
        # NOTE: the parentheses matter. In PowerShell ',' binds LOOSER than
        # -f, so a bare "'a', 'b' -f $x" inside an array literal parses as
        # ('a','b') -f $x, which yields a nested array. Stop-Aigit would then
        # print that whole nested array on one line and the message turns to
        # mush. Parenthesise every -f element.
        ('Find out who has it:  netstat -ano | findstr :{0}    (last column is the PID)' -f $PortNumber),
        'Then either:',
        '  1) pick another port:  .\aigit.ps1 -Port 8888',
        '  2) stop whatever is holding that port and try again.',
        '',
        'Often the holder is an aigit from a previous run that did not exit.',
        'In that case just start aigit again; it is fine.'
    )
}

# --------------------------------------------------------------------------- run

$sandboxArg = @()
if ($Sandbox) { $sandboxArg = @('--sandbox') }

$exe = $pyCmd[0]
$pre = @()
if ($pyCmd.Count -gt 1) { $pre = @($pyCmd[1..($pyCmd.Count - 1)]) }

Write-Host ''
Write-Host ('-' * 64) -ForegroundColor DarkGray
Write-Host '  aigit'
Say-Note ("python    {0}" -f ($pyCmd -join ' '))
Say-Note ("repo      {0}" -f $Repo)
if ($Sandbox) { Say-Note 'mode      sandbox (wiped and rebuilt)' }
else { Say-Note 'mode      existing repo (mind the Discard button)' }
Say-Note ("open      http://127.0.0.1:{0}/" -f $PortNumber)
Say-Note 'stop      Ctrl+C in this window'
Say-Note 'note      no browser is opened; copy the URL above yourself'
Write-Host ('-' * 64) -ForegroundColor DarkGray
Write-Host ''

& $exe @pre $server --root $Repo --port $PortNumber @sandboxArg @ServerArgs
$rc = $LASTEXITCODE

if ($rc -eq 0) {
    Write-Host ''
    Write-Host 'aigit stopped.'
    if ($Pause) { $null = Read-Host 'Press Enter to close' }
    exit 0
}

Write-Host ''
Write-Host ("[error] server.py exited with code {0}." -f $rc) -ForegroundColor Red
if ($rc -eq 1) {
    Say-Note 'If the traceback above mentions "Address already in use" or'
    Say-Note '"WinError 10048", the port was taken between the check and the bind.'
    Say-Note ("Try again on a different port:  .\aigit.ps1 -Port 8888")
} else {
    Say-Note 'Report the traceback above along with how you started aigit.'
}
Write-Host ''
if ($Pause) { $null = Read-Host 'Press Enter to close' }
exit $rc
