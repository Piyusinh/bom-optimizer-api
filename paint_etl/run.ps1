# Starts the local database (Docker) and the loader app.  Usage:  .\run.ps1
# Needs: Docker Desktop, Python 3.12 or newer on PATH (`python --version`).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

docker compose up -d                              # PostgreSQL on localhost:5433 (container bom-paint-postgres)

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
}
Write-Host "Open http://localhost:8010" -ForegroundColor Green
& .\.venv\Scripts\python.exe run.py
