#!/usr/bin/env bash
# Frontend build + macOS ASR service bundle, run by Tauri's `beforeBuildCommand`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."            # front-end/desktop-ui

pnpm build
bash "$SCRIPT_DIR/bundle_asr.sh"
