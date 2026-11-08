<#
.SYNOPSIS
    Runs the SVPI weekly workflow (called by Windows Task Scheduler).
.DESCRIPTION
    Imports new files from data\incoming, generates the weekly report (or a
    DATA MISSING report requesting a fresh MT5 upload) and writes a log to
    logs\scheduler_YYYYMMDD_HHMMSS.log. Exit code: 0 ok, 2 data missing, 1 error.
#>
[CmdletBinding()]
param([string[]]$ExtraArgs = @())
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs") | Out-Null
$Log = Join-Path $Root ("logs\scheduler_{0:yyyyMMdd_HHmmss}.log" -f (Get-Date))
$env:SVPI_ENV = "production"
& $Py (Join-Path $Root "run_weekly.py") @ExtraArgs *>&1 | Tee-Object -FilePath $Log
$code = $LASTEXITCODE
if ($code -eq 2) {
    Write-Warning "DATA MISSING: export the latest MT5 report and place it in data\incoming. See $Log"
} elseif ($code -ne 0) {
    Write-Error "SVPI weekly run failed (exit $code). See $Log"
}
exit $code
