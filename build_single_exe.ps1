param(
    [string]$Python = ".venv\Scripts\python.exe",
    [switch]$InstallDependencies
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Warning "The one-file build is retired because it extracts 97 MB on every launch. Building the stable one-folder app instead."
& "$root\build_app.ps1" -Python $Python -InstallDependencies:$InstallDependencies
