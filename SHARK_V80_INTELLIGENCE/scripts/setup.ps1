<#
.SYNOPSIS
    One-time setup of SHARK V80 Performance Intelligence on Windows.
.DESCRIPTION
    Creates a Python virtual environment in .venv, installs dependencies,
    creates the data/report folders and runs the automated test suite.
    Run from the project folder:
        powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
#>
[CmdletBinding()]
param(
    [string]$Python = "py",          # Python launcher; use "python" if py is not installed
    [switch]$WithAI,                 # also install the optional anthropic package
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host "SVPI setup in $Root"

# Python 3.12+ check
$ver = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.12") {
    throw "Python 3.12 or newer is required (found $ver). Install it from python.org."
}

if (-not (Test-Path ".venv")) {
    & $Python -m venv .venv
}
$Py = Join-Path $Root ".venv\Scripts\python.exe"
& $Py -m pip install --upgrade pip
& $Py -m pip install -r requirements.txt
if ($WithAI) { & $Py -m pip install -r requirements-ai.txt }

& $Py -c "import config; config.ensure_directories(); print('Folders ready')"

if (-not $SkipTests) {
    & $Py -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed - do not use for production until fixed." }
}
Write-Host ""
Write-Host "Setup complete."
Write-Host "  Dashboard:      .\scripts\start_dashboard.ps1"
Write-Host "  Weekly run:     .\scripts\run_weekly.ps1"
Write-Host "  Schedule:       .\scripts\register_weekly_task.ps1   (run as your user)"
