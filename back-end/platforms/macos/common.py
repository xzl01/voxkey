# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Shared types for the macOS dual-engine ASR service."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol


def resolve_asset(base: Path, p: str) -> Path:
    """Resolve an asset path from config against a base directory.

    * ``~/...`` is expanded via :func:`Path.expanduser`.
    * an already-absolute path is used as-is (portable across machines).
    * a relative path is joined onto ``base`` (the macos platform dir), so the
      project no longer hard-codes ``~/Dev/qwen-asr`` and works wherever the
      repo lives.
    """
    expanded = Path(p).expanduser()
    if expanded.is_absolute():
        return expanded
    return (base / p).resolve()


class EngineKind(str, Enum):
    """Identifies which compute backend an ASR engine runs on."""

    FUNASR_NCE = "funasr_coreml"  # Apple Neural Engine via Core ML
    QWEN3_GPU = "qwen3_gpu"  # Apple GPU via llama.cpp Metal


@dataclass
class Transcript:
    """Result of a single transcription: text plus provenance/diagnostics."""

    text: str
    engine: EngineKind
    latency_s: float
    compute_units: str = ""  # "ane" / "gpu" / "cpu"
    ok: bool = True
    error: str | None = None
    # optional diagnostics from the NCE path
    ane_hit_rate: float | None = None


@dataclass
class EngineConfig:
    """Per-engine configuration (enabled flag, timeout, free-form extras)."""

    name: EngineKind
    enabled: bool = True
    timeout_s: float = 20.0
    extra: dict = field(default_factory=dict)


class ASREngine(Protocol):
    """Interface every ASR backend implements (used by the orchestrator)."""

    kind: EngineKind

    def transcribe(
        self, waveform: "np.ndarray", *, language: str | None = None, timeout_s: float | None = None
    ) -> Transcript: ...

    def warmup(self) -> None: ...

    def shutdown(self) -> None: ...


# re-export for convenience
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    import numpy as np
