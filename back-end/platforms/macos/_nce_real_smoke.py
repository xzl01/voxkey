"""End-to-end smoke test of the REAL SenseVoice NCE engine.

Loads models/funasr_coreml (real ONNX + am.mvn + tokens.txt), runs the
WavFrontend -> ORT-CoreML -> CTC decode pipeline on a synthetic 16 kHz
signal. Proves the full link runs on the ANE without crashing. Real-speech
transcription is validated separately via service.py with a real wav.
"""

from __future__ import annotations
import sys
import time
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from funasr_coreml import FunASRCoreML

sr = 16000
# 2 s synthetic "speech-like" signal: a few formant-ish tones, amplitude-modulated
t = np.arange(sr * 2) / sr
sig = np.zeros_like(t, dtype=np.float32)
for f, a in ((180, 0.6), (320, 0.3), (900, 0.15)):
    sig += a * np.sin(2 * np.pi * f * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))
sig = (sig / np.max(np.abs(sig)) * 0.7).astype(np.float32)

for cu in ("ane", "cpu"):
    try:
        eng = FunASRCoreML(str(Path("models/funasr_coreml")), compute_units=cu)
        t0 = time.perf_counter()
        tr = eng.transcribe(sig, language="auto")
        dt = time.perf_counter() - t0
        print(f">>> [{cu}] providers={eng._session.get_providers()}")
        print(
            f">>> [{cu}] ok={tr.ok} latency={tr.latency_s:.2f}s decode={dt:.2f}s text={tr.text!r}"
        )
        print(
            f">>> [{cu}] NCE ENGINE OK"
            if tr.ok
            else f">>> [{cu}] ENGINE RETURNED ERROR: {tr.error}"
        )
        eng.shutdown()
    except Exception as e:
        print(f">>> [{cu}] FAILED: {e!r}")
