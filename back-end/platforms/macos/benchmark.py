# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Diagnostics + benchmark for the macOS dual-engine ASR.

Reports per-engine latency, memory footprint, and which compute unit each
engine used (ANE for FunASR Core ML, GPU for Qwen3-ASR). Also prints how to
measure the true ANE hit rate with Instruments.

Usage:
  python benchmark.py --wav sample.wav
  python benchmark.py --seconds 5        # synthesize a 5s tone and run
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    args = ap.parse_args()

    import soundfile as sf
    from orchestrator import DualEngineOrchestrator
    from macos_daemon import Daemon

    if args.wav:
        wav, _ = sf.read(args.wav, dtype="float32", always_2d=False)
        if wav.ndim > 1:
            wav = wav.mean(axis=1)
    else:
        sr = 16_000
        t = np.linspace(0, args.seconds, int(sr * args.seconds), endpoint=False)
        wav = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    daemon = Daemon(cfg)
    orch: DualEngineOrchestrator = daemon.orch

    report = {"waveform_seconds": round(len(wav) / 16_000, 2), "engines": {}}
    if orch.funasr:
        tr = orch.funasr.transcribe(wav)
        report["engines"]["funasr_coreml"] = {
            "latency_s": round(tr.latency_s, 3),
            "compute_units": tr.compute_units,
            "text": tr.text[:60],
            "ok": tr.ok,
            "error": tr.error,
        }
    if orch.qwen3:
        tr = orch.qwen3.transcribe(wav)
        report["engines"]["qwen3_gpu"] = {
            "latency_s": round(tr.latency_s, 3),
            "compute_units": tr.compute_units,
            "text": tr.text[:60],
            "ok": tr.ok,
            "error": tr.error,
        }
    if orch.funasr and orch.qwen3:
        res = orch.transcribe(wav)
        report["fused"] = res.to_json()

    try:
        import psutil

        proc = psutil.Process()
        report["rss_mb"] = round(proc.memory_info().rss / 1e6, 1)
    except Exception:
        pass

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        "\nTo measure the true ANE hit rate (fraction of ops on the Neural "
        "Engine), run under Instruments:\n"
        "  xcrun xctrace record --template 'Metal System Trace' \\\n"
        "    --attach --pid $(pgrep -f service.py) --output trace.trace\n"
        "or use the Core ML 'os_signpost' intervals and inspect the ANE "
        "counter in the 'com.apple.neural.engine' category."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
