#!/usr/bin/env bash
# Frontend build + platform ASR service bundle, run by Tauri's `beforeBuildCommand`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."            # front-end/desktop-ui

pnpm build

# Bundle the platform-specific ASR service into src-tauri/asr. macOS uses the
# bash bundle script; Windows uses the PowerShell one. Linux has no bundled
# backend (the CI frontend job only type-checks), so it is skipped.
OS="$(uname -s)"
case "$OS" in
  Darwin*)
    bash "$SCRIPT_DIR/bundle_asr.sh"
    ;;
  *MINGW**|*MSYS**|*CYGWIN*|*Windows_NT*)
    if command -v pwsh >/dev/null 2>&1; then
      pwsh "$SCRIPT_DIR/bundle_asr_win.ps1" "$SCRIPT_DIR"
    elif command -v powershell >/dev/null 2>&1; then
      powershell -ExecutionPolicy Bypass -File "$SCRIPT_DIR/bundle_asr_win.ps1" "$SCRIPT_DIR"
    else
      echo "!! no PowerShell found; skipping Windows ASR bundle"
    fi
    ;;
  *)
    echo "==> skipping ASR bundle on unsupported OS: $OS"
    ;;
esac
