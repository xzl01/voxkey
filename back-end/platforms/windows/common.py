# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Shared types for the Windows dual-engine ASR service.

Mirrors back-end/platforms/macos/common.py but renames the FunASR engine kind
to ``funasr_directml`` because on Windows SenseVoice runs via ONNX Runtime's
DirectML Execution Provider (no Core ML / Apple Neural Engine on Windows).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol


def resolve_asset(base: Path, p: str) -> Path:
    """Resolve an asset path from config against a base directory.

    * ``~/...`` is expanded via :func:`Path.expanduser`.
    * an already-absolute path is used as-is (portable across machines).
    * a relative path is joined onto ``base`` (the windows platform dir).
    """
    expanded = Path(p).expanduser()
    if expanded.is_absolute():
        return expanded
    return (base / p).resolve()


class EngineKind(str, Enum):
    """Identifies which compute backend an ASR engine runs on."""

    FUNASR_DML = "funasr_directml"  # AMD GPU via ONNX Runtime DirectML
    QWEN3_GPU = "qwen3_gpu"  # AMD GPU via llama.cpp Vulkan


@dataclass
class Transcript:
    """Result of a single transcription: text plus provenance/diagnostics."""

    text: str
    engine: EngineKind
    latency_s: float
    compute_units: str = ""  # "dml" / "gpu" / "cpu"
    ok: bool = True
    error: str | None = None


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
