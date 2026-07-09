# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Qwen3-ASR on the Apple GPU via llama.cpp's Metal backend.

This is the *GPU* half of the dual-engine setup. It reuses the project's own
``qwen_asr_gguf`` package (the same one the Linux daemon loads). On macOS,
llama.cpp compiles its compute shaders to **Metal**, which executes on the
Apple GPU — NOT the Neural Engine. (The NCE is reserved for the FunASR Core ML
path; see funasr_coreml.py.)

Because Qwen3-ASR is an autoregressive speech LLM, it must run here on the GPU;
the ANE cannot schedule its dynamic decode loop. We still honor requirement 4
by loading the int4 GGUF weights (already quantized), keeping memory + latency
low on the unified-memory Apple Silicon GPU.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import numpy as np

from common import EngineKind, Transcript

logger = logging.getLogger("voxkey.qwen3_gpu")


class Qwen3GPU:
    kind = EngineKind.QWEN3_GPU

    def __init__(
        self,
        model_dir: str,
        *,
        project_dir: str | None = None,
        onnx_provider: str = "CPU",
        llm_use_gpu: bool = True,
        encoder_frontend_fn: str = "qwen3_asr_encoder_frontend.int4.onnx",
        encoder_backend_fn: str = "qwen3_asr_encoder_backend.int4.onnx",
        llm_fn: str = "qwen3_asr_llm.q4_k.gguf",
        enable_aligner: bool = False,
        timeout_s: float = 20.0,
    ) -> None:
        self.model_dir = Path(model_dir).expanduser()
        self.project_dir = Path(project_dir).expanduser() if project_dir else self.model_dir.parent
        self.onnx_provider = onnx_provider
        self.llm_use_gpu = llm_use_gpu
        self.encoder_frontend_fn = encoder_frontend_fn
        self.encoder_backend_fn = encoder_backend_fn
        self.llm_fn = llm_fn
        self.enable_aligner = enable_aligner
        self.timeout_s = timeout_s
        self._engine = None
        self._load()

    def _load(self) -> None:
        """Import and instantiate the qwen_asr_gguf QwenASREngine (Metal GPU)."""
        from audio import waveform_to_wav_bytes  # noqa: F401  (ensure importable)

        bin_dir = self.project_dir / "qwen_asr_gguf" / "inference" / "bin"
        os.environ.setdefault("GGML_VK_DISABLE_F16", "1")
        old_ld = os.environ.get("LD_LIBRARY_PATH", "")
        if str(bin_dir) not in old_ld.split(":"):
            os.environ["LD_LIBRARY_PATH"] = f"{bin_dir}:{old_ld}" if old_ld else str(bin_dir)
        sys.path.insert(0, str(self.project_dir))
        from qwen_asr_gguf.inference import ASREngineConfig, QwenASREngine

        logger.info("Loading Qwen3-ASR GPU engine (Metal): %s", self.model_dir)
        self._engine = QwenASREngine(
            config=ASREngineConfig(
                model_dir=str(self.model_dir),
                onnx_provider=self.onnx_provider,
                llm_use_gpu=self.llm_use_gpu,
                encoder_frontend_fn=self.encoder_frontend_fn,
                encoder_backend_fn=self.encoder_backend_fn,
                llm_fn=self.llm_fn,
                enable_aligner=self.enable_aligner,
                verbose=False,
            )
        )
        logger.info("Qwen3-ASR GPU engine ready")

    def warmup(self) -> None:
        """Run one short dummy transcription to initialize the Metal GPU engine."""
        import soundfile as sf
        import tempfile

        dummy = np.zeros(int(16_000 * 0.5), dtype=np.float32)
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            sf.write(f.name, dummy, 16_000)
            try:
                self._engine.transcribe(
                    audio_file=f.name,
                    language=None,
                    context="",
                    start_second=0,
                    duration=None,
                    temperature=0.4,
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("Qwen3 warmup skipped: %s", exc)

    def transcribe(
        self, waveform: np.ndarray, *, language: str | None = None, timeout_s: float | None = None
    ) -> Transcript:
        """Transcribe a 16 kHz mono waveform on the GPU; returns a Transcript."""
        import soundfile as sf
        import tempfile

        t0 = time.perf_counter()
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                sf.write(f.name, waveform.astype(np.float32), 16_000)
                path = f.name
            # The engine expects a concrete language name or None for auto; "auto"
            # is not accepted by its validate_language(), so normalise it.
            norm_lang = None if (language is None or str(language).lower() == "auto") else language
            result = self._engine.transcribe(
                audio_file=path,
                language=norm_lang,
                context="",
                start_second=0,
                duration=None,
                temperature=0.4,
            )
            text = (getattr(result, "text", "") or "").strip()
            return Transcript(
                text=text, engine=self.kind, latency_s=time.perf_counter() - t0, compute_units="gpu"
            )
        except Exception as exc:
            return Transcript(
                text="",
                engine=self.kind,
                latency_s=time.perf_counter() - t0,
                compute_units="gpu",
                ok=False,
                error=repr(exc),
            )
        finally:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass

    def shutdown(self) -> None:
        """Release the underlying qwen_asr_gguf engine and its GPU resources."""
        if self._engine is not None:
            try:
                self._engine.shutdown()
            except Exception:
                pass
            self._engine = None
