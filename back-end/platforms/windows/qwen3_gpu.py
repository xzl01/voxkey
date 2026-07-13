# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Qwen3-ASR on an AMD GPU via llama.cpp's Vulkan backend (Windows).

Windows counterpart of back-end/platforms/macos/qwen3_gpu.py. The decoder runs
on the GPU through llama.cpp's **Vulkan** backend (AMD Adrenalin ships the
Vulkan runtime), and the ONNX encoder runs on the provider selected by config
(``Dml`` when the DirectML runtime is chosen, otherwise ``CPU``).

Reuses the project's own ``qwen_asr_gguf`` package. On Windows the prebuilt
llama.cpp DLLs (``llama.dll`` / ``ggml-vulkan.dll``) live in
``qwen_asr_gguf/inference/bin/`` and are added to the DLL search path.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

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
        llm_fn: str = "qwen3_asr_llm.q4_k_m.gguf",
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
        """Import and instantiate the qwen_asr_gguf QwenASREngine (Vulkan GPU)."""
        bin_dir = self.project_dir / "qwen_asr_gguf" / "inference" / "bin"
        # Make the prebuilt llama.cpp DLLs discoverable on Windows.
        if bin_dir.is_dir():
            os.environ["PATH"] = f"{bin_dir};{os.environ.get('PATH', '')}"
            try:
                os.add_dll_directory(str(bin_dir))
            except (AttributeError, OSError, FileNotFoundError):
                pass
        # GGML_VK_DISABLE_F16 is for Intel iGPU FP16 overflow; RDNA3 (Radeon
        # 780M) Vulkan FP16 is generally stable — leave configurable via env.
        os.environ.setdefault("GGML_VK_DISABLE_F16", "1")
        sys.path.insert(0, str(self.project_dir))
        from qwen_asr_gguf.inference import ASREngineConfig, QwenASREngine

        logger.info("Loading Qwen3-ASR GPU engine (Vulkan): %s", self.model_dir)
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
        """Run one short dummy transcription to initialize the Vulkan GPU engine."""
        dummy = np.zeros(int(16_000 * 0.5), dtype=np.float32)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            sf.write(f.name, dummy, 16_000)
            path = f.name
        try:
            self._engine.transcribe(
                audio_file=path,
                language=None,
                context="",
                start_second=0,
                duration=None,
                temperature=0.4,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("Qwen3 warmup skipped: %s", exc)
        finally:
            Path(path).unlink(missing_ok=True)

    def transcribe(
        self, waveform: np.ndarray, *, language: str | None = None, timeout_s: float | None = None
    ) -> Transcript:
        """Transcribe a 16 kHz mono waveform on the GPU; returns a Transcript."""
        t0 = time.perf_counter()
        path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                sf.write(f.name, waveform.astype(np.float32), 16_000)
                path = f.name
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
            if path:
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
