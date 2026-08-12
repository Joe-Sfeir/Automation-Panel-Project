param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "Installing build/runtime dependencies..."
& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt pyinstaller

Write-Host "Building one-file executable..."
& $Python -m PyInstaller --clean --noconfirm app/automation_onefile.spec

Write-Host ""
Write-Host "Build complete."
Write-Host "Executable: $root\\dist\\TechnicatAutomation.exe"
