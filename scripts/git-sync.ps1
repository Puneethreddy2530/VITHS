# Fetch -> pull (rebase) -> push on origin/main
# Usage (from anywhere):  powershell -File "C:\path\to\VITHS\scripts\git-sync.ps1"
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
Write-Host "Repo: $root" -ForegroundColor Cyan

git fetch origin
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

git pull --rebase origin main
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

git push origin main
exit $LASTEXITCODE
