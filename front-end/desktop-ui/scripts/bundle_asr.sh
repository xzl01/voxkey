#!/usr/bin/env bash
# Bundle the macOS ASR service (Python + modules + capture_helper) into
# src-tauri/asr so Tauri can copy it into the .app's Resources/asr. The Rust
# launcher (`start_asr_service`) then runs <Resources>/asr/python/bin/python3
# service.py, which is what makes the app self-contained (no system Python or
# manual `setup.sh` required on the end-user's Mac).
#
# This runs from `beforeBuildCommand` (scripts/before-build.sh). It is macOS
# only; the DMG workflow builds on macos-14.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SRC="$REPO_ROOT/back-end/platforms/macos"
DEST="$SCRIPT_DIR/../src-tauri/asr"

echo "==> Bundling macOS ASR service: $SRC -> $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"

# 1. Python service modules + config + the *bundle* requirements (the
#    pip-installable runtime subset; the vendored qwen_asr_gguf is NOT pip-installed
#    — see requirements-bundle.txt). We also copy the dev requirements.txt for
#    reference / parity.
for f in service.py audio.py orchestrator.py common.py funasr_coreml.py qwen3_gpu.py config.json requirements.txt requirements-bundle.txt; do
  if [[ -f "$SRC/$f" ]]; then
    cp "$SRC/$f" "$DEST/$f"
  fi
done

# 1b. Model downloaders + shared fetch helper + per-asset SHA-256 manifests.
#     The app fetches the (large) model weights from GitHub Release at first
#     launch rather than bundling them (see start_asr_service in lib.rs), so
#     these must ship inside Resources/asr alongside service.py.
for f in _fetch.py ensure_funasr.py ensure_qwen3.py; do
  if [[ -f "$SRC/$f" ]]; then
    cp "$SRC/$f" "$DEST/$f"
  fi
done
if [[ -d "$SRC/manifests" ]]; then
  mkdir -p "$DEST/manifests"
  cp "$SRC/manifests"/*.json "$DEST/manifests/" 2>/dev/null || true
fi

# 1c. Third-party license / attribution file (Apache 2.0 + FunASR MODEL_LICENSE
#     + Qwen3-ASR + llama.cpp). Shipped so the self-contained DMG satisfies the
#     redistribution attribution requirements of the bundled/downloaded assets.
if [[ -f "$SRC/THIRD_PARTY_LICENSES" ]]; then
  cp "$SRC/THIRD_PARTY_LICENSES" "$DEST/THIRD_PARTY_LICENSES"
fi

# 2. Compile the Swift mic/hotkey helpers next to service.py (it resolves
#    `capture_helper` as a sibling of service.py).
if command -v swiftc >/dev/null 2>&1; then
  swiftc -O "$SRC/capture_helper.swift" -o "$DEST/capture_helper" 2>/dev/null \
    && chmod 0755 "$DEST/capture_helper" \
    || echo "!! capture_helper compile failed; mic capture will not work"
else
  echo "!! swiftc not found; capture_helper not bundled (mic capture will fail)"
fi

# 3. Relocatable Python. Prefer python-build-standalone (truly relocatable, so
#    the .app runs on any Mac); fall back to a venv from system python.
PY_DEST="$DEST/python"
if [[ -d "$PY_DEST" ]]; then
  echo "==> Reusing existing bundled Python at $PY_DEST"
else
  PBS_RELEASE="${PBS_RELEASE:-20241024}"
  PBS_TARBALL="cpython-3.12.7+${PBS_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
  PBS_URL="${PBS_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_TARBALL}}"
  TMP="$(mktemp -d)"
  echo "==> Downloading relocatable Python: $PBS_URL"
  if curl -fSL "$PBS_URL" -o "$TMP/python.tar.gz"; then
    tar -xzf "$TMP/python.tar.gz" -C "$TMP"
    mv "$TMP/python" "$PY_DEST"
  else
    echo "!! python-build-standalone download failed; falling back to system venv"
    python3 -m venv "$PY_DEST"
  fi
  rm -rf "$TMP"
fi

# 4. Install Python requirements into the bundled interpreter.
"$PY_DEST/bin/python3" -m pip install --upgrade pip
# Enable the Metal backend for llama-cpp-python (Qwen3 GPU engine). Harmless for
# packages that don't use CMake; required when no prebuilt Metal wheel is fetched.
export CMAKE_ARGS="-DGGML_METAL=on"
"$PY_DEST/bin/python3" -m pip install -r "$DEST/requirements-bundle.txt"

echo "==> ASR bundle ready at $DEST"
