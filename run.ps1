# Starts Web-API-Poster on http://127.0.0.1:8000 (Windows PowerShell 5.1)
# Usage: powershell -ExecutionPolicy Bypass -File .\run.ps1
#        -LogFile <path>  also write the server log to a file (used by install-autostart.ps1)
param([string]$LogFile = "")
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment .venv ..."
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Python not found. Install Python 3.12 and enable 'Add python.exe to PATH'." }
}

Write-Host "Installing dependencies ..."
& .\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if (-not (Test-Path ".\.env")) {
    Copy-Item ".\.env.example" ".\.env"
    Write-Host "Created .env from .env.example"
}

if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Write-Warning "ffprobe not found: video checks are disabled. Install ffmpeg (see README)."
}

if ($LogFile) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null
    $env:LOG_FILE = $LogFile
}

Write-Host "Open http://127.0.0.1:8000 in your browser. Stop: Ctrl+C"
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
