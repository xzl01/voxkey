# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Acquire the Qwen3-ASR int4 GGUF weights for the GPU (Metal) backend.

The Qwen3-ASR GPU engine (qwen3_gpu.py) loads the int4 encoder + LLM GGUF files
shipped by the qwen-asr project. Those weights are large and downloaded on first
launch rather than committed to the repo (per the architecture's "model files
are selected and installed after first launch" rule).

This helper checks for the required files and, if missing, downloads them from
the configured ModelScope/HuggingFace mirror. Set VOXKEY_QWEN3_URLS to override.

Usage:
  python ensure_qwen3.py --out models/qwen3_asr
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


# Mirror the file names published on HuggingFace (nzyaltair/Qwen3-ASR-0.6B-gguf).
# NOTE: the LLM is quantised as q4_k, not int4 — keep this in sync with the
# ``llm_fn`` default in qwen3_gpu.py / ASREngineConfig.llm_fn.
REQUIRED = [
    "qwen3_asr_encoder_frontend.int4.onnx",
    "qwen3_asr_encoder_backend.int4.onnx",
    "qwen3_asr_llm.q4_k.gguf",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-url", default=os.environ.get("VOXKEY_QWEN3_URLS", ""))
    args = ap.parse_args()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    missing = [f for f in REQUIRED if not (out / f).exists()]
    if not missing:
        print(f"Qwen3-ASR weights present at {out}")
        return 0
    if not args.base_url:
        print("MISSING (download skipped, no --base-url):")
        for f in missing:
            print(f"  {out / f}")
        print("\nSet --base-url (a directory URL) or place the files manually:")
        for f in REQUIRED:
            print(f"  {f}")
        return 1
    try:
        import urllib.request
    except ImportError:
        return 1
    for f in missing:
        url = args.base_url.rstrip("/") + "/" + f
        dst = out / f
        print(f"Downloading {url} -> {dst}")
        urllib.request.urlretrieve(url, dst)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
