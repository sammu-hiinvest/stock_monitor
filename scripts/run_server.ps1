# Starts the local dashboard web server.
# Usage: powershell -ExecutionPolicy Bypass -File .\scripts\run_server.ps1
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
