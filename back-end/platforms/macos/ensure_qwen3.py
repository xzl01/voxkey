# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Acquire the Qwen3-ASR int4 GGUF weights for the GPU (Metal) backend.

The Qwen3-ASR GPU engine (qwen3_gpu.py) loads the int4 encoder + LLM GGUF files
shipped by the qwen-asr project. Those weights are large and downloaded on first
launch rather than committed to the repo (per the architecture's "model files
are selected and installed after first launch" rule).

The default source is the upstream HuggingFace repo. A China-accessible mirror
(hf-mirror.com) is tried automatically as a fallback, so this works without a
manual --base-url. Override either with VOXKEY_QWEN3_URLS / VOXKEY_QWEN3_MIRRORS.

Usage:
  python ensure_qwen3.py --out models/qwen3_asr
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from _fetch import fetch_one

HF_BASE = "https://huggingface.co/nzyaltair/Qwen3-ASR-0.6B-gguf/resolve/main"
HF_MIRROR = "https://hf-mirror.com/nzyaltair/Qwen3-ASR-0.6B-gguf/resolve/main"

REQUIRED = [
    "qwen3_asr_encoder_frontend.int4.onnx",
    "qwen3_asr_encoder_backend.int4.onnx",
    "qwen3_asr_llm.q4_k.gguf",
]


def _mirrors(explicit: str | None) -> list[str]:
    if explicit:
        return [m for m in explicit.split(",") if m]
    env = os.environ.get("VOXKEY_QWEN3_MIRRORS", "")
    if env:
        return [m for m in env.split(",") if m]
    return [HF_MIRROR]


def _manifest(explicit: str | None) -> dict[str, str]:
    """Optional filename -> sha256 map for integrity verification.

    Source precedence: explicit --manifest path, then VOXKEY_QWEN3_SHA256S
    (a JSON object string), then an empty map (no verification).
    """
    raw = explicit or os.environ.get("VOXKEY_QWEN3_SHA256S", "")
    if not raw:
        return {}
    import json

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items()}
    except json.JSONDecodeError:
        print("!! invalid SHA256 manifest; skipping verification")
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-url", default=os.environ.get("VOXKEY_QWEN3_URLS", HF_BASE))
    ap.add_argument("--mirrors", default=None, help="comma-separated fallback URLs")
    ap.add_argument(
        "--manifest",
        default=None,
        help="path to JSON file mapping each weight filename to its expected SHA-256",
    )
    args = ap.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    missing = [f for f in REQUIRED if not (out / f).exists()]
    if not missing:
        print(f"Qwen3-ASR weights present at {out}")
        return 0

    mirrors = _mirrors(args.mirrors)
    manifest = _manifest(args.manifest)
    print(f"Downloading {len(missing)} Qwen3-ASR file(s) from {args.base_url}")
    if mirrors:
        print(f"  mirrors: {mirrors}")
    if manifest:
        print(f"  sha256 manifest: {len(manifest)} entry(ies)")
    for f in missing:
        fetch_one(
            f,
            out / f,
            base_url=args.base_url,
            mirrors=mirrors,
            sha256=manifest.get(f),
        )
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
