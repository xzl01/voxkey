# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
    Bundle the Windows ASR service (Python + modules + llama.cpp Vulkan DLLs) into
    src-tauri/asr so Tauri can copy it into the installed app's resource directory.
    The Rust launcher (start_asr_service) then runs <Resources>/asr/python/python.exe
    service.py, making the Windows build self-contained (no system Python required).

    This is the Windows counterpart of scripts/bundle_asr.sh (macOS). It is invoked
    from scripts/before-build.sh on non-macOS hosts.

    The (large) model weights are NOT bundled: like macOS they are fetched from the
    GitHub release on first launch (start_model_download -> bootstrap_assets.py).

.PARAMETER ScriptDir
    The directory of this script (front-end/desktop-ui/scripts). Passed by
    before-build.sh so we can resolve repo-relative paths regardless of CWD.
#>
[CmdletBinding()]
param(
    [string]$ScriptDir = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'

$SCRIPT_DIR = if ($ScriptDir) { $ScriptDir } else { $PSScriptRoot }
$DESKTOP_UI = Resolve-Path (Join-Path $SCRIPT_DIR '..')
$REPO_ROOT   = Resolve-Path (Join-Path $SCRIPT_DIR '..\..\..')
$SRC         = Join-Path $REPO_ROOT 'back-end\platforms\windows'
$DEST        = Join-Path $DESKTOP_UI 'src-tauri\asr'

Write-Host "==> Bundling Windows ASR service: $SRC -> $DEST"

if (-not (Test-Path $SRC)) {
    throw "Windows ASR source directory not found: $SRC"
}

# Allow skipping the (slow) Python + pip bundling on a quick compile check.
$BUNDLE_PYTHON = $env:BUNDLE_PYTHON
if ($BUNDLE_PYTHON -eq $null) { $BUNDLE_PYTHON = '1' }

# 1. Prepare destination. Preserve any existing bundled Python so re-runs
#    (e.g. after a config-only change) don't re-download ~1GB of wheels.
if (-not (Test-Path $DEST)) { New-Item -ItemType Directory -Force -Path $DEST | Out-Null }
# Refresh the service modules / config / qwen package each run.
foreach ($item in @(
    'service.py', 'common.py', 'funasr_directml.py', 'qwen3_gpu.py',
    'orchestrator.py', 'windows_daemon.py', 'run_backend.py',
    'bootstrap_assets.py', 'convert_funasr_coreml.py',
    'config.json', 'requirements.txt', 'qwen-asr'
)) {
    $p = Join-Path $DEST $item
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}

# 2. Python service modules + config. Copy the runtime modules (skip dev/diag
#    helpers and standalone test scripts).
$Modules = @(
    'service.py', 'common.py', 'funasr_directml.py', 'qwen3_gpu.py',
    'orchestrator.py', 'windows_daemon.py', 'run_backend.py',
    'bootstrap_assets.py', 'convert_funasr_coreml.py'
)
foreach ($m in $Modules) {
    $srcFile = Join-Path $SRC $m
    if (Test-Path $srcFile) { Copy-Item $srcFile (Join-Path $DEST $m) }
}

# 2b. Ship a bundle config (relative paths) so it resolves next to service.py.
#     config.example.json already uses asr_project_dir "qwen-asr" + model_dir
#     "models", both relative to the service script directory.
$exampleCfg = Join-Path $SRC 'config.example.json'
if (Test-Path $exampleCfg) {
    Copy-Item $exampleCfg (Join-Path $DEST 'config.json')
} elseif (Test-Path (Join-Path $SRC 'config.json')) {
    Copy-Item (Join-Path $SRC 'config.json') (Join-Path $DEST 'config.json')
}

# 2c. Reference requirements file (for parity / manual inspection).
$req = Join-Path $SRC 'requirements.txt'
if (Test-Path $req) { Copy-Item $req (Join-Path $DEST 'requirements.txt') }

# 2d. On-demand heavy-stack requirements (FunASR/torch), installed at first use
#     by funasr_directml._ensure_funasr_stack(). Not installed at build time.
$reqFun = Join-Path $SRC 'requirements-funasr.txt'
if (Test-Path $reqFun) { Copy-Item $reqFun (Join-Path $DEST 'requirements-funasr.txt') }

# 3. The qwen_asr_gguf Python package (encoders + llama.cpp Vulkan DLLs). This is
#    what asr_project_dir "qwen-asr" resolves to; copying the whole package keeps
#    the import graph (asr.py / llama.py / encoder.py ...) intact. Exclude VCS /
#    cache dirs so the installer stays lean.
$GGUF_SRC = Join-Path $SRC 'qwen-asr\qwen_asr_gguf'
$GGUF_DST = Join-Path $DEST 'qwen-asr\qwen_asr_gguf'
if (Test-Path $GGUF_SRC) {
    New-Item -ItemType Directory -Force -Path $GGUF_DST | Out-Null
    # robocopy is present on all supported Windows; /E copies subdirs, /XD excludes
    # the given directories. Robocopy returns exit 1 when files are copied
    # (not an error), so we don't fail the build on it.
    robocopy $GGUF_SRC $GGUF_DST /E /XD .git __pycache__ /XF *.pyc | Out-Null
    Write-Host "==> Copied qwen_asr_gguf package (encoders + Vulkan DLLs)"
} else {
    Write-Warning "qwen_asr_gguf not found at $GGUF_SRC; Qwen3 engine will not load"
}

# 4. Relocatable Python (python-build-standalone, Windows x64). Mirrors the macOS
#    bundle's relocatable-Python approach so the installed app needs no system
#    Python. Skipped when BUNDLE_PYTHON != 1 (e.g. a fast compile check).
if ($BUNDLE_PYTHON -eq '1') {
    $PY_DEST = Join-Path $DEST 'python'
    if (Test-Path $PY_DEST) {
        Write-Host "==> Reusing existing bundled Python at $PY_DEST"
    } else {
        $PBS_RELEASE = $env:PBS_RELEASE
        if (-not $PBS_RELEASE) { $PBS_RELEASE = '20260623' }
        $PBS_TARBALL = "cpython-3.12.13+$PBS_RELEASE-x86_64-pc-windows-msvc-install_only.tar.gz"
        $PBS_URL = $env:PBS_URL
        if (-not $PBS_URL) {
            $PBS_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_RELEASE/$PBS_TARBALL"
        }
        $TMP = Join-Path $env:TEMP ("voxkey-pbs-" + [System.Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $TMP | Out-Null
        try {
            Write-Host "==> Downloading relocatable Python: $PBS_URL"
            Invoke-WebRequest -Uri $PBS_URL -OutFile (Join-Path $TMP 'python.tar.gz') -UseBasicParsing
            # python-build-standalone Windows layout extracts to a top-level
            # `python/` dir containing python.exe at the root.
            # Use the Windows tar.exe explicitly: Git-bash's tar is earlier on
            # PATH and mis-parses the Windows temp path ("C:\...") as a remote
            # host ("C:"). Windows tar.exe handles the native path fine.
            $WinTar = Join-Path $env:SystemRoot 'System32\tar.exe'
            & $WinTar -xzf (Join-Path $TMP 'python.tar.gz') -C $TMP
            $extracted = Join-Path $TMP 'python'
            if (-not (Test-Path (Join-Path $extracted 'python.exe'))) {
                # Some releases nest under an extra folder; locate python.exe.
                $found = Get-ChildItem -Path $TMP -Recurse -Filter 'python.exe' | Select-Object -First 1
                if ($found) { $extracted = $found.DirectoryName }
            }
            Move-Item $extracted $PY_DEST
        } finally {
            Remove-Item -Recurse -Force $TMP -ErrorAction SilentlyContinue
        }
    }

    # 5. Install the service runtime requirements into the bundled interpreter.
    #    CPU-only torch (avoid the multi-GB CUDA build); the llama.cpp DLLs are
    #    shipped alongside (step 3), loaded via ctypes, so no llama_cpp pip wheel.
    $PY_EXE = Join-Path $PY_DEST 'python.exe'
    Write-Host "==> Installing Python service requirements into $PY_EXE"
    & $PY_EXE -m pip install --upgrade pip
    # Lightweight deps bundled into the installer. The heavy ML stack
    # (torch + funasr and their transitive deps: transformers, modelscope,
    # jieba, sklearn, scipy, sympy, llvmlite, numba ...) is intentionally NOT
    # installed here: it is ~1 GB and only used by the FunASR DirectML engine,
    # which fetches it on first use via funasr_directml._ensure_funasr_stack().
    # The Qwen3 engine works fully offline with just the deps below.
    & $PY_EXE -m pip install `
        numpy soundfile sounddevice fastapi "uvicorn[standard]" `
        onnxruntime-directml keyboard pyperclip plyer requests
    if ($LASTEXITCODE -ne 0) {
        throw "pip install of Windows ASR base requirements failed (exit $LASTEXITCODE)"
    }
}

Write-Host "==> Windows ASR bundle ready at $DEST"
