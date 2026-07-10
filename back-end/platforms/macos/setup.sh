#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
#
# Bootstrap the VoxKey macOS ASR service on Apple Silicon.
#
# What this does:
#   1. Install Homebrew system deps (ffmpeg, python, portaudio, rust).
#   2. Create a Python venv and install macOS ASR requirements.
#   3. Ensure the FunASR Core ML (NCE) model. Prefers a one-shot download of the
#      pre-converted package; falls back to a local torch export if offline.
#   4. Download the Qwen3-ASR GGUF (GPU) weights from HuggingFace (with an
#      hf-mirror.com fallback) -- no manual URL needed.
#
# Run from the repo root:
#   bash back-end/platforms/macos/setup.sh
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MACOS_DIR="$REPO_ROOT/back-end/platforms/macos"
VENV="$MACOS_DIR/.venv"
# Python 3.11 or 3.12 works (coremltools needs <3.13). Override with PYTHON=... if needed.
PYTHON="${PYTHON:-python3.11}"

echo "==> Repo root: $REPO_ROOT"
echo "==> macOS dir: $MACOS_DIR"

# 1. system deps --------------------------------------------------------------
if command -v brew >/dev/null 2>&1; then
  echo "==> Installing Homebrew formulas (Brewfile)"
  brew bundle --file="$MACOS_DIR/Brewfile" || echo "brew bundle had warnings; continuing"
else
  echo "!! Homebrew not found. Install it from https://brew.sh then re-run." >&2
  exit 1
fi

# 2. venv + python deps -------------------------------------------------------
echo "==> Creating venv at $VENV"
"$PYTHON" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip
pip install -r "$MACOS_DIR/requirements.txt"

# 3. FunASR -> Core ML (NCE) --------------------------------------------------
# Prefer the pre-converted package (one download, no torch needed); fall back
# to a local torch export if the package source is unavailable.
MODEL_FAIL=0
if [[ "${SKIP_FUNASR_CONVERT:-0}" != "1" ]]; then
  echo "==> Ensuring FunASR Core ML model (NCE)"
  if ! python "$MACOS_DIR/ensure_funasr.py" --out "$MACOS_DIR/models/funasr_coreml"; then
    if [[ "${FUNASR_LOCAL_CONVERT:-0}" == "1" ]]; then
      echo "!! FunASR download failed; installing local-conversion deps then retrying."
      pip install -r "$MACOS_DIR/requirements-convert.txt"
      python "$MACOS_DIR/ensure_funasr.py" --out "$MACOS_DIR/models/funasr_coreml" \
        || MODEL_FAIL=1
    else
      echo "!! FunASR model setup failed; re-run with FUNASR_LOCAL_CONVERT=1"
      echo "   to build it locally (needs requirements-convert.txt)."
      MODEL_FAIL=1
    fi
  fi
fi

# 4. Qwen3-ASR GGUF (GPU) -----------------------------------------------------
# Downloaded from HuggingFace (with hf-mirror.com fallback) -- no manual URL.
if [[ "${SKIP_QWEN_DOWNLOAD:-0}" != "1" ]]; then
  echo "==> Ensuring Qwen3-ASR GGUF is present"
  python "$MACOS_DIR/ensure_qwen3.py" --out "$MACOS_DIR/models/qwen3_asr" || MODEL_FAIL=1
fi

if [[ "$MODEL_FAIL" != "0" ]]; then
  echo "!! Setup incomplete: one or more ASR models are missing." >&2
  echo "   The service will not run until models are available." >&2
  exit 1
fi

echo "==> Setup complete. Activate with: source $VENV/bin/activate"
echo "==> Run service:     python $MACOS_DIR/service.py"
echo "==> Run daemon:      python $MACOS_DIR/macos_daemon.py --config $MACOS_DIR/config.json"
