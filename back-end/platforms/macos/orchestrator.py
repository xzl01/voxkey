# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Run FunASR (NCE) and Qwen3-ASR (GPU) concurrently and fuse their output.

Both engines receive the same 16 kHz mono waveform. FunASR on the Neural Engine
returns in tens-to-hundreds of ms; Qwen3-ASR on the GPU takes longer but is
usually more accurate. We let them run in parallel and then fuse:

  * fast_first : surface the NCE result first, replace it with the GPU result
                 once it lands (lowest perceived latency + best final quality).
  * best       : pick the GPU transcript if it succeeded, else the NCE one.
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
    """Run the FunASR (NCE) and Qwen3-ASR (GPU) engines concurrently and fuse."""

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
        for engine, key in (
            (self.funasr, EngineKind.FUNASR_NCE),
            (self.qwen3, EngineKind.QWEN3_GPU),
        ):
            th = threading.Thread(target=_run, args=(engine, key), daemon=True)
            threads.append(th)
            th.start()

        # For fast_first we resolve as soon as the NCE engine returns; we still
        # wait for the GPU engine in the background for the refined pass.
        for th in threads:
            th.join(timeout=self.funasr.timeout_s if self.funasr else 30)
            with lock:
                if EngineKind.FUNASR_NCE in results:
                    break

        # Wait out the GPU engine if it hasn't finished yet (it usually hasn't).
        for th in threads:
            th.join()

        with lock:
            fa = results.get(EngineKind.FUNASR_NCE)
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
                EngineKind.FUNASR_NCE if fa else EngineKind.QWEN3_GPU
            )
        if self.fusion.mode == "best":
            if qw_ok:
                return qw.text, EngineKind.QWEN3_GPU
            return fa.text, EngineKind.FUNASR_NCE
        # fast_first (default): NCE first, GPU refines if it improves agreement
        if qw_ok and fa_ok:
            ratio = difflib.SequenceMatcher(None, fa.text, qw.text).ratio()
            if ratio >= 0.6 or len(qw.text) >= len(fa.text):
                return qw.text, EngineKind.QWEN3_GPU
            return fa.text, EngineKind.FUNASR_NCE
        if fa_ok:
            return fa.text, EngineKind.FUNASR_NCE
        return qw.text, EngineKind.QWEN3_GPU
