# Start PS-003 API + dashboard (from project root).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Py = "$Root\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Error "No venv found. Run first:  .\scripts\bootstrap-venv.ps1"
}

Write-Host "Starting PS-003 via start.py  (open http://localhost:8888)"
& $Py "$Root\start.py" @args
