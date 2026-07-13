# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
    Launch the VoxKey Windows voice-input daemon (and optionally the local ASR API).

.DESCRIPTION
    Resolves the platform venv (or falls back to a system `python`), sets the
    llama.cpp / ONNX Runtime compatibility flags, and runs windows_daemon.py
    with the given config. Pass -Service to also start the dual-engine FastAPI
    ASR service (service.py) that the daemon talks to over HTTP. Any extra
    arguments are forwarded to the daemon.

.EXAMPLE
    .\run.ps1                       # uses config.json next to this script
    .\run.ps1 --self-test           # run the self-check without listening
    .\run.ps1 -Service              # start the local /transcribe API first
    .\run.ps1 --config my.json      # use an explicit config
#>
[CmdletBinding()]
param(
    [string]$Config = (Join-Path $PSScriptRoot 'config.json'),
    [switch]$Service
)

$ErrorActionPreference = 'Stop'

$WinDir = $PSScriptRoot
$Venv = Join-Path $WinDir '.venv'

if (Test-Path (Join-Path $Venv 'Scripts\python.exe')) {
    $Py = Join-Path $Venv 'Scripts\python.exe'
} else {
    $Py = 'python'
}

# llama.cpp Vulkan flag. Originally added for Intel iGPU FP16 overflow; RDNA3
# (Radeon 780M) Vulkan FP16 is generally stable, so it can be unset to test for
# higher speed. Left ON here for safety; override with $env:GGML_VK_DISABLE_F16='0'.
if (-not $env:GGML_VK_DISABLE_F16) { $env:GGML_VK_DISABLE_F16 = '1' }

# Limit ONNX Runtime / OpenMP threads so the FunASR encoder fallback doesn't
# starve the system on a shared-memory APU. 4 is a sane default for 8-core APUs.
if (-not $env:OMP_NUM_THREADS) { $env:OMP_NUM_THREADS = '4' }

if ($Service) {
    Write-Host "[run] starting local dual-engine ASR service on http://127.0.0.1:17863"
    Start-Process -FilePath $Py -ArgumentList (Join-Path $WinDir 'service.py') -WorkingDirectory $WinDir
    Start-Sleep -Seconds 2
}

& $Py (Join-Path $WinDir 'windows_daemon.py') --config $Config @args
