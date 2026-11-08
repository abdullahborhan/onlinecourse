<#
.SYNOPSIS
    Starts the SVPI Streamlit dashboard on http://localhost:8501 (local only).
    Use -Demo to open the clearly labelled SYNTHETIC demo database instead.
#>
[CmdletBinding()]
param([switch]$Demo, [int]$Port = 8501)
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
$env:SVPI_ENV = if ($Demo) { "demo" } else { "production" }
& $Py -m streamlit run app.py --server.port $Port --server.address 127.0.0.1
