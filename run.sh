#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${QWEN_VOICE_INPUT_CONFIG:-$SCRIPT_DIR/config.json}"
ASR_PROJECT="${QWEN_ASR_PROJECT_DIR:-$HOME/AI/Model/Qwen3-ASR-GGUF}"
VENV="${QWEN_ASR_VENV:-$HOME/qwen3-asr-venv}"
PYTHON_BIN="${QWEN_ASR_PYTHON:-}"

if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$VENV/bin/python3" ]]; then
    PYTHON_BIN="$VENV/bin/python3"
  else
    PYTHON_BIN="python3"
  fi
fi

export GGML_VK_DISABLE_F16="${GGML_VK_DISABLE_F16:-1}"
BIN_DIR="$ASR_PROJECT/qwen_asr_gguf/inference/bin"
if [[ -d "$BIN_DIR" ]]; then
  export LD_LIBRARY_PATH="$BIN_DIR:${LD_LIBRARY_PATH:-}"
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/voice_input_daemon.py" --config "$CONFIG" "$@"
