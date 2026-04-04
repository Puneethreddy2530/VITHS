# Create .venv and install dependencies (PyTorch CUDA wheels first, then the rest).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "Project root: $Root"

if (-not (Test-Path "$Root\.venv\Scripts\python.exe")) {
    Write-Host "Creating .venv ..."
    python -m venv "$Root\.venv"
}

$Py = "$Root\.venv\Scripts\python.exe"
$Pip = "$Root\.venv\Scripts\pip.exe"

& $Py -m pip install --upgrade pip

Write-Host "Installing PyTorch (CUDA 11.8 wheels) ..."
& $Py -m pip install torch==2.7.1+cu118 torchvision==0.22.1+cu118 `
    --index-url https://download.pytorch.org/whl/cu118

Write-Host "Installing requirements-base.txt ..."
& $Py -m pip install -r "$Root\requirements-base.txt"

Write-Host "Done. Activate with:  .\.venv\Scripts\Activate.ps1"
Write-Host "Run backend with:     .\scripts\run-backend.ps1"
