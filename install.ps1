<#
.SYNOPSIS
    Install claude-link (Windows).

.DESCRIPTION
    This script does one thing: find a Python 3.10+ interpreter and hand over to
    link/install.py, which is where the actual installation lives -- one
    implementation for every host application and every platform, with tests
    behind it instead of three copies that drift.

    Every argument is passed straight through:
      -Agent auto|claude|codex|both   default: whatever is on this machine
      -SkipHook                       no notification hook
      -SelfTest smoke|suite|all|none  default: smoke, about a second
      -Dev                            editable install, for working on this

      powershell -ExecutionPolicy Bypass -File install.ps1 -Help    the full list
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'claude', 'codex', 'both')]
    [string]$Agent = 'auto',
    [switch]$SkipHook,
    [ValidateSet('smoke', 'suite', 'all', 'none')]
    [string]$SelfTest = 'smoke',
    [switch]$NoDiagnostics,
    [switch]$Dev,
    [switch]$Quiet,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
# The installer lives at the repository root, so its own directory *is* the
# package root. Resolving anything relative to the parent breaks the moment the
# project is cloned under a different name.
$LinkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

$candidates = @()
foreach ($name in @('python', 'python3')) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) { $candidates += $found.Source }
}
$candidates += @(
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
    "C:\Program Files\Python313\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Program Files\Python311\python.exe"
)

$Python = $null
foreach ($candidate in $candidates) {
    if (-not $candidate -or -not (Test-Path $candidate)) { continue }
    # The Microsoft Store stubs are zero-length reparse points, not interpreters.
    if ((Get-Item $candidate).Length -eq 0) { continue }
    # Native stderr must not become a terminating error here: a stub that
    # prints to stderr would otherwise abort the search instead of being
    # skipped.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $candidate -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" 2>$null
    $usable = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEAP
    if ($usable) { $Python = $candidate; break }
}

if (-not $Python) {
    Write-Host 'X   No Python 3.10+ found.' -ForegroundColor Red
    Write-Host '    Install it with:  winget install --id Python.Python.3.12 --scope user'
    exit 1
}

$forwarded = @()
if ($Help) {
    $forwarded += '--help'
} else {
    $forwarded += @('--agent', $Agent, '--self-test', $SelfTest)
    if ($SkipHook)      { $forwarded += '--skip-hook' }
    if ($NoDiagnostics) { $forwarded += '--no-diagnostics' }
    if ($Dev)           { $forwarded += '--dev' }
    if ($Quiet)         { $forwarded += '--quiet' }
}

Push-Location $LinkRoot
try {
    # pip and unittest both write progress to stderr, which Windows PowerShell
    # wraps in ErrorRecords and, with ErrorActionPreference='Stop', turns into
    # an aborted installer. Relax it around the one call that matters.
    $ErrorActionPreference = 'Continue'
    & $Python -X utf8 -m link.install @forwarded
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
