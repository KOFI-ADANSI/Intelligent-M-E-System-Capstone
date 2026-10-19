$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$appDirectory = Join-Path $PSScriptRoot "app"

if (-not (Test-Path $python)) {
    throw "Project Python environment not found: $python"
}

$securePassword = Read-Host "PostgreSQL password" -AsSecureString
$env:NSME_DB_PASSWORD = [System.Net.NetworkCredential]::new("", $securePassword).Password

try {
    & $python -m uvicorn main:app --app-dir $appDirectory --port 8000
    if ($LASTEXITCODE -ne 0) {
        throw "Uvicorn exited with code $LASTEXITCODE."
    }
}
finally {
    Remove-Item Env:\NSME_DB_PASSWORD -ErrorAction SilentlyContinue
}
