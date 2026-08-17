param(
    [string]$Python = ".venv\Scripts\python.exe",
    [switch]$InstallDependencies
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if ($InstallDependencies) {
    Write-Host "Installing build/runtime dependencies..."
    & $Python -m pip install -r requirements.txt pyinstaller
}

Write-Host "Building fast-start one-folder application..."
& $Python -m PyInstaller --clean --noconfirm app/automation_onedir.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

Write-Host ""
Write-Host "Build complete."
Write-Host "Application: $root\dist\TechnicatAutomation\TechnicatAutomation.exe"
