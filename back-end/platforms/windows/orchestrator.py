# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Run FunASR (DirectML) and Qwen3-ASR (Vulkan GPU) concurrently and fuse.

Ported from back-end/platforms/macos/orchestrator.py. On Windows both engines
target the same AMD GPU (Radeon 780M): FunASR via ONNX Runtime DirectML,
Qwen3-ASR via llama.cpp Vulkan. They share system memory, so we keep a single
uvicorn worker and run the two engines in parallel threads.
"""

from __future__ import annotations

import difflib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from common import EngineKind, Transcript

logger = logging.getLogger("voxkey.orchestrator")


@dataclass
class FusionConfig:
    """Strategy + thresholds for combining the two engine outputs."""

    mode: str = "fast_first"
    funasr_priority_until_s: float = 1.2
    min_agreement_chars: int = 4


@dataclass
class DualResult:
    """Fused outcome: final text, both engine transcripts, the chosen engine, total latency."""

    final_text: str
    funasr: Transcript | None
    qwen3: Transcript | None
    chosen: EngineKind
    total_s: float

    def to_json(self) -> dict:
        return {
            "text": self.final_text,
            "chosen_engine": self.chosen.value,
            "total_latency_s": round(self.total_s, 3),
            "funasr": _t(self.funasr),
            "qwen3": _t(self.qwen3),
        }


def _t(t: Transcript | None) -> dict | None:
    if t is None:
        return None
    return {
        "text": t.text,
        "latency_s": round(t.latency_s, 3),
        "compute_units": t.compute_units,
        "ok": t.ok,
        "error": t.error,
    }


class DualEngineOrchestrator:
    """Run the FunASR (DirectML) and Qwen3-ASR (Vulkan GPU) engines concurrently and fuse."""

    def __init__(self, funasr, qwen3, fusion: FusionConfig | None = None) -> None:
        self.funasr = funasr
        self.qwen3 = qwen3
        self.fusion = fusion or FusionConfig()

    def transcribe(
        self,
        waveform: np.ndarray,
        *,
        language: str | None = None,
        on_partial: Callable[[Transcript], None] | None = None,
    ) -> DualResult:
        """Transcribe ``waveform`` on both engines in parallel and fuse.

        ``on_partial`` (if given) is called with each engine's result as soon as
        it lands, enabling low-latency streaming before the final fused text.
        """
        t0 = time.perf_counter()
        results: dict[EngineKind, Transcript] = {}
        lock = threading.Lock()

        def _run(engine, key):
            tr = engine.transcribe(waveform, language=language)
            with lock:
                results[key] = tr
            if on_partial is not None:
                on_partial(tr)

        threads = []
        funasr_key = getattr(self.funasr.kind, "value", None) if self.funasr else None
        qwen3_key = getattr(self.qwen3.kind, "value", None) if self.qwen3 else None
        for engine, key in (
            (self.funasr, EngineKind.FUNASR_DML),
            (self.qwen3, EngineKind.QWEN3_GPU),
        ):
            if engine is None:
                continue
            th = threading.Thread(target=_run, args=(engine, key), daemon=True)
            threads.append(th)
            th.start()

        # For fast_first we resolve as soon as the FunASR engine returns; we still
        # wait for the GPU engine in the background for the refined pass.
        for th in threads:
            th.join(timeout=getattr(self.funasr, "timeout_s", 30) if self.funasr else 30)
            with lock:
                if EngineKind.FUNASR_DML in results:
                    break

        # Wait out the GPU engine if it hasn't finished yet (it usually hasn't).
        for th in threads:
            th.join()

        with lock:
            fa = results.get(EngineKind.FUNASR_DML)
            qw = results.get(EngineKind.QWEN3_GPU)

        final_text, chosen = self._fuse(fa, qw)
        total = time.perf_counter() - t0
        return DualResult(final_text=final_text, funasr=fa, qwen3=qw, chosen=chosen, total_s=total)

    def _fuse(self, fa: Transcript | None, qw: Transcript | None) -> tuple[str, EngineKind]:
        """Pick the final text + engine according to ``self.fusion.mode``."""
        fa_ok = fa and fa.ok and fa.text.strip()
        qw_ok = qw and qw.ok and qw.text.strip()
        if not fa_ok and not qw_ok:
            return (fa.text if fa else (qw.text if qw else "")), (
                EngineKind.FUNASR_DML if fa else EngineKind.QWEN3_GPU
            )
        if self.fusion.mode == "best":
            if qw_ok:
                return qw.text, EngineKind.QWEN3_GPU
            return fa.text, EngineKind.FUNASR_DML
        # fast_first (default): FunASR first, Qwen3 refines if it improves agreement
        if qw_ok and fa_ok:
            ratio = difflib.SequenceMatcher(None, fa.text, qw.text).ratio()
            if ratio >= 0.6 or len(qw.text) >= len(fa.text):
                return qw.text, EngineKind.QWEN3_GPU
            return fa.text, EngineKind.FUNASR_DML
        if fa_ok:
            return fa.text, EngineKind.FUNASR_DML
        return qw.text, EngineKind.QWEN3_GPU
