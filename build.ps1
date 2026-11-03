# Build the Windows executable and (optionally) a desktop shortcut.
#   powershell -ExecutionPolicy Bypass -File build.ps1 [-Shortcut] [-SkipTests]
param([switch]$Shortcut, [switch]$SkipTests)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv\Scripts\python.exe")) { py -3 -m venv .venv }
& .venv\Scripts\python.exe -m pip install -q -r requirements-dev.txt
if (-not (Test-Path "assets\triagent.ico")) { & .venv\Scripts\python.exe scripts\make_icon.py }
if (-not $SkipTests) {
    $env:QT_QPA_PLATFORM = "offscreen"
    & .venv\Scripts\python.exe -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed - build aborted" }
    Remove-Item Env:QT_QPA_PLATFORM
}
# PyInstaller erases dist\TriAgentStudio: never build while a project workspace lives there
& .venv\Scripts\python.exe scripts\check_build_safety.py
if ($LASTEXITCODE -ne 0) { throw "Build aborted to protect project files (see message above)" }
& .venv\Scripts\pyinstaller.exe TriAgentStudio.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$exe = Join-Path $PSScriptRoot "dist\TriAgentStudio\TriAgentStudio.exe"
Write-Host "Built: $exe"
if ($Shortcut) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut((Join-Path $desktop "TriAgent Studio.lnk"))
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = Split-Path $exe
    $lnk.IconLocation = "$exe,0"
    $lnk.Save()
    Write-Host "Desktop shortcut created."
}
