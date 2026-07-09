#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
#
# Bootstrap the VoxKey macOS ASR service on Apple Silicon.
#
# What this does:
#   1. Install Homebrew system deps (ffmpeg, python, portaudio, rust).
#   2. Create a Python venv and install macOS ASR requirements.
#   3. (Optional) Pull + convert the FunASR model to Core ML for the NCE path.
#   4. (Optional) Download the Qwen3-ASR GGUF weights for the GPU path.
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
# Converts Paraformer/SenseVoice ONNX exports to .mlpackage with int8/fp16
# quantization so the encoder runs on the Apple Neural Engine.
if [[ "${SKIP_FUNASR_CONVERT:-0}" != "1" ]]; then
  echo "==> Converting FunASR model to Core ML (NCE)"
  python "$MACOS_DIR/convert_funasr_coreml.py" \
    --model sensevoice_small \
    --out "$MACOS_DIR/models/funasr_coreml" \
    --quantize int8 || echo "!! FunASR conversion failed; run manually. Continuing."
fi

# 4. Qwen3-ASR GGUF (GPU) -----------------------------------------------------
# The llama.cpp Metal backend loads the int4 GGUF shipped by the qwen-asr project.
if [[ "${SKIP_QWEN_DOWNLOAD:-0}" != "1" ]]; then
  echo "==> Ensuring Qwen3-ASR GGUF is present"
  python "$MACOS_DIR/ensure_qwen3.py" --out "$MACOS_DIR/models/qwen3_asr" || \
    echo "!! Qwen3-ASR download skipped/failed; set model_dir in config manually."
fi

echo "==> Setup complete. Activate with: source $VENV/bin/activate"
echo "==> Run service:     python $MACOS_DIR/service.py"
echo "==> Run daemon:      python $MACOS_DIR/macos_daemon.py --config $MACOS_DIR/config.json"
