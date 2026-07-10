# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Acquire the FunASR (SenseVoice) Core ML package for the NCE (ANE) backend.

The NCE engine (funasr_coreml.FunASRCoreML) needs four files in model_dir:
    model.onnx, am.mvn, tokens.txt, frontend.json
These are normally produced by convert_funasr_coreml.py -- a heavy torch/funasr
export. To avoid that on end-user machines, a pre-converted package is published
as a tarball and downloaded here, with automatic mirror fallback.

If no download source is reachable, this falls back to a local torch conversion
unless --no-convert is given.

Override the source with VOXKEY_FUNASR_URLS / VOXKEY_FUNASR_MIRRORS.

Usage:
  python ensure_funasr.py --out models/funasr_coreml
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from _fetch import fetch_archive

# Default: pre-converted package published as a GitHub release asset.
RELEASE_BASE = "https://github.com/xzl01/voxkey/releases/download/voxkey-models-v1"
ARCHIVE = "funasr_coreml.tar.gz"

FILES = ["model.onnx", "am.mvn", "tokens.txt", "frontend.json"]


def _mirrors(explicit: str | None) -> list[str]:
    if explicit:
        return [m for m in explicit.split(",") if m]
    env = os.environ.get("VOXKEY_FUNASR_MIRRORS", "")
    if env:
        return [m for m in env.split(",") if m]
    return []


def _download_package(out: Path, base_url: str, mirrors: list[str], sha256: str | None) -> bool:
    try:
        fetch_archive(ARCHIVE, out, base_url=base_url, mirrors=mirrors, sha256=sha256)
    except Exception as exc:  # noqa: BLE001
        print(f"!! package download failed: {exc}")
        return False
    missing = [f for f in FILES if not (out / f).exists()]
    if missing:
        print(f"!! package extracted but missing files: {missing}")
        return False
    return True


def _local_convert(out: Path, script_dir: Path) -> bool:
    print("==> Falling back to local conversion (requires torch/funasr)...")
    cmd = [
        sys.executable,
        str(script_dir / "convert_funasr_coreml.py"),
        "--model",
        "iic/SenseVoiceSmall",
        "--out",
        str(out),
        "--quantize",
        "int8",
    ]
    try:
        subprocess.run(cmd, check=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"!! local conversion failed: {exc}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-url", default=os.environ.get("VOXKEY_FUNASR_URLS", RELEASE_BASE))
    ap.add_argument("--mirrors", default=None)
    ap.add_argument(
        "--sha256",
        default=os.environ.get("VOXKEY_FUNASR_SHA256", ""),
        help="expected SHA-256 of the package archive (verified before extraction)",
    )
    ap.add_argument(
        "--no-convert",
        action="store_true",
        help="do not fall back to local conversion if download fails",
    )
    args = ap.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    if all((out / f).exists() for f in FILES):
        print(f"FunASR Core ML package present at {out}")
        return 0

    mirrors = _mirrors(args.mirrors)
    sha = args.sha256 or None
    if args.base_url and _download_package(out, args.base_url, mirrors, sha):
        print("Done.")
        return 0

    if args.no_convert:
        print("!! FunASR model not available (download failed, --no-convert set).")
        return 1
    return 0 if _local_convert(out, Path(__file__).resolve().parent) else 1


if __name__ == "__main__":
    raise SystemExit(main())
