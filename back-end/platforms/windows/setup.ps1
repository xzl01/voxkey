# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
    Bootstrap the VoxKey Windows development / runtime environment.

.DESCRIPTION
    Installs the system dependencies needed to build the Tauri desktop app and
    run the Windows voice-input daemon:
      * Visual Studio Build Tools + Windows SDK (MSVC)  - required by Tauri
      * Python 3.12 (standalone; `uv` trampoline is unreliable on this box)
      * ffmpeg (optional, for audio diagnostics)
    Then creates a Python venv and installs back-end/platforms/windows/requirements.txt.

.NOTES
    Run from anywhere; it resolves the windows platform dir via $PSScriptRoot.
    The VS Build Tools step triggers a UAC prompt and needs administrator rights.
    If your profile is on a restricted/cloud-synced volume, install CPython to a
    plain path (e.g. C:\Python312) and the script will pick it up automatically.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$WinDir = $PSScriptRoot
$RepoRoot = Resolve-Path (Join-Path $WinDir '..\..')
$Venv = Join-Path $WinDir '.venv'

function Test-Command($cmd) {
    return [bool](Get-Command $cmd -ErrorAction SilentlyContinue)
}

# --- 1. MSVC (Visual Studio Build Tools + Windows SDK) -------------------
# Tauri v2 on Windows needs the MSVC toolchain and the Windows SDK.
$cl = Get-ChildItem 'C:\Program Files\Microsoft Visual Studio' -Recurse -Filter cl.exe -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $cl) {
    Write-Host '==> Installing Visual Studio Build Tools + Windows SDK (MSVC) ...'
    winget install Microsoft.VisualStudio.2022.BuildTools --silent `
        --accept-package-agreements --accept-source-agreements `
        --override "--add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.Windows10SDK.22621 --add Microsoft.VisualStudio.Component.Windows11SDK.22621"
} else {
    Write-Host "==> MSVC already present: $($cl.FullName)"
}

# --- 2. Standalone Python 3.12 -------------------------------------------
# Avoid `uv` (its trampoline fails to spawn Python on untrusted reparse points).
$PyExe = $null
foreach ($d in @(
        "$env:LOCALAPPDATA\Programs\Python\Python312",
        'C:\Python312',
        "$env:USERPROFILE\AppData\Local\Programs\Python\Python312")) {
    $cand = Join-Path $d 'python.exe'
    if (Test-Path $cand) { $PyExe = $cand; break }
}
if (-not $PyExe) {
    Write-Host '==> Installing Python 3.12 (winget) ...'
    winget install Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    # Re-detect after install
    foreach ($d in @(
            "$env:LOCALAPPDATA\Programs\Python\Python312",
            'C:\Python312',
            "$env:USERPROFILE\AppData\Local\Programs\Python\Python312")) {
        $cand = Join-Path $d 'python.exe'
        if (Test-Path $cand) { $PyExe = $cand; break }
    }
}
if (-not $PyExe) { $PyExe = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $PyExe) { throw 'Python 3.12 not found after install. Add it to PATH and re-run.' }
Write-Host "==> Using Python: $PyExe"
& $PyExe --version

# --- 3. ffmpeg (optional) ------------------------------------------------
if (-not (Test-Command ffmpeg)) {
    Write-Host '==> Installing ffmpeg (optional) ...'
    winget install Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
} else {
    Write-Host '==> ffmpeg already present'
}

# --- 4. venv + Python requirements ---------------------------------------
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    Write-Host "==> Creating venv at $Venv"
    & $PyExe -m venv $Venv
}
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
& $VenvPy -m pip install --upgrade pip
& $VenvPy -m pip install -r (Join-Path $WinDir 'requirements.txt')

Write-Host ''
Write-Host '==> Setup complete.'
Write-Host "    Activate :  & '$Venv\Scripts\Activate.ps1'"
Write-Host "    Run daemon: & '$Venv\Scripts\python.exe' '$WinDir\windows_daemon.py' --config '$WinDir\config.json'"
