#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${BUILD_DIR:-$SCRIPT_DIR/build}"
PREFIX="${PREFIX:-$HOME/.local}"
BUILD_TYPE="${CMAKE_BUILD_TYPE:-RelWithDebInfo}"

cmake -S "$SCRIPT_DIR" -B "$BUILD_DIR" \
  -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
  -DCMAKE_INSTALL_PREFIX="$PREFIX"
cmake --build "$BUILD_DIR"
cmake --install "$BUILD_DIR"

LIB_PATH="$PREFIX/lib/fcitx5/libvoxkeyinput.so"
if [[ ! -e "$LIB_PATH" ]]; then
  LIB_PATH="$(find "$PREFIX" -path '*/fcitx5/libvoxkeyinput.so' -print -quit)"
fi
if [[ -z "$LIB_PATH" || ! -e "$LIB_PATH" ]]; then
  echo "Could not find installed libvoxkeyinput.so under $PREFIX" >&2
  exit 1
fi

CONF_DIR="$HOME/.local/share/fcitx5/addon"
mkdir -p "$CONF_DIR"
sed "s|^Library=.*|Library=${LIB_PATH%.so}|" \
  "$SCRIPT_DIR/voxkeyinput.conf" > "$CONF_DIR/voxkeyinput.conf"

echo "Installed fcitx5 addon config: $CONF_DIR/voxkeyinput.conf"
echo "Restart fcitx5 with: fcitx5 -rd"
