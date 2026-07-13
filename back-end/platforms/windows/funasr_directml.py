# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""FunASR (SenseVoice) running on an AMD GPU via ONNX Runtime DirectML.

Windows counterpart of back-end/platforms/macos/funasr_coreml.py. Core ML is
Apple-only, so on Windows we dispatch the SenseVoice ONNX encoder through
ONNX Runtime's **DirectML Execution Provider**, which targets any DX12 GPU
(including the integrated Radeon 780M). Unsupported ops fall back to CPU.

Pipeline per utterance (identical to the Core ML variant):
  1. WavFrontend -> 80-dim fbank, LFR stack (m=7) -> 560-dim features + CMVN.
  2. ONNX encoder consumes speech[B,T,560] + speech_lengths + language +
     textnorm, returns ctc_logits[B,T',V].
  3. CTC greedy decode (collapse repeats + blank) -> BPE pieces -> text.

Interface matches ``ASREngine`` so it can be dropped into the
``DualEngineOrchestrator`` unchanged.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

from common import EngineKind, Transcript

logger = logging.getLogger("voxkey.funasr_directml")


def _ensure_funasr_stack() -> None:
    """Lazily install the heavy FunASR/torch stack on first use.

    These packages (~1 GB) are intentionally NOT bundled into the installer so
    the Windows MSI/NSIS stays small; they are fetched on demand the first time
    the FunASR DirectML engine is constructed. The Qwen3 engine does not need
    them and works fully offline. If the network is unavailable the import
    simply fails and the caller degrades gracefully (FunASR = None).
    """
    try:
        import torch  # noqa: F401
        from funasr.frontends.wav_frontend import WavFrontend  # noqa: F401
        return
    except ImportError:
        pass
    import os
    import subprocess
    import sys

    logger.info("FunASR stack (torch + funasr) not bundled; installing on first use...")
    req = os.path.join(os.path.dirname(__file__), "requirements-funasr.txt")
    args = [sys.executable, "-m", "pip", "install"]
    if os.path.isfile(req):
        args += ["-r", req]
    else:
        args += ["funasr", "torch", "--extra-index-url", "https://download.pytorch.org/whl/cpu"]
    try:
        subprocess.check_call(args)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - network
        raise RuntimeError(
            "FunASR 依赖 (torch + funasr) 首次使用需联网下载，但安装失败。"
            "请检查网络后重试，或仅使用 Qwen3 引擎（无需该依赖）。"
        ) from exc
    # Re-import to confirm the install succeeded.
    import torch  # noqa: F401
    from funasr.frontends.wav_frontend import WavFrontend  # noqa: F401


# SenseVoice default frontend (matches iic/SenseVoiceSmall export).
_DEFAULT_FRONTEND = {
    "fs": 16000,
    "window": "hamming",
    "n_mels": 80,
    "frame_length": 25,
    "frame_shift": 10,
    "lfr_m": 7,
    "lfr_n": 6,
    "dither": 0.0,
    "snip_edges": False,
}
_DEFAULT_LID = {"auto": 0, "zh": 3, "en": 4, "yue": 7, "ja": 11, "ko": 12, "nospeech": 13}
_DEFAULT_TEXTNORM = {"withitn": 14, "woitn": 15}
_BLANK_ID = 0


def _load_vocab(model_dir: Path) -> list[str]:
    p = model_dir / "tokens.txt"
    if p.is_file():
        return [ln.rstrip("\n") for ln in p.read_text(encoding="utf-8").splitlines()]
    return []


def _load_frontend_cfg(model_dir: Path) -> dict:
    p = model_dir / "frontend.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover
            logger.warning("frontend.json unreadable (%s); using defaults", exc)
    return {
        "frontend_conf": dict(_DEFAULT_FRONTEND),
        "cmvn_file": "am.mvn",
        "lid_dict": dict(_DEFAULT_LID),
        "textnorm_dict": dict(_DEFAULT_TEXTNORM),
        "default_language": 0,
        "default_textnorm": 15,
    }


class FunASRDirectML:
    """SenseVoice encoder on an AMD GPU via ONNX Runtime DirectML EP."""

    kind = EngineKind.FUNASR_DML

    def __init__(
        self, model_path: str, *, compute_units: str = "dml", timeout_s: float = 8.0
    ) -> None:
        _ensure_funasr_stack()
        import onnxruntime as ort

        self.model_dir = Path(model_path)
        self.timeout_s = timeout_s

        onnx_file = self.model_dir / "model.onnx"
        if not onnx_file.is_file():
            raise FileNotFoundError(f"DirectML ONNX missing: {onnx_file}")

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Cap intra-op threads so the encoder fallback doesn't starve the system
        # on shared-memory APUs (Radeon 780M).
        so.intra_op_num_threads = 4
        self._session = ort.InferenceSession(
            str(onnx_file), sess_options=so, providers=self._providers(compute_units)
        )

        cfg = _load_frontend_cfg(self.model_dir)
        self._lid = cfg.get("lid_dict", _DEFAULT_LID)
        self._textnorm = cfg.get("textnorm_dict", _DEFAULT_TEXTNORM)
        self._default_lang = cfg.get("default_language", 0)
        self._default_textnorm = cfg.get("default_textnorm", 15)
        self._vocab = _load_vocab(self.model_dir)
        self._frontend = self._build_frontend(cfg)

        ins = [i.name for i in self._session.get_inputs()]
        outs = [o.name for o in self._session.get_outputs()]
        self._speech_name = self._pick(ins, ("speech",))
        self._len_name = self._pick(ins, ("speech_lengths",))
        self._lang_name = self._pick(ins, ("language",))
        self._tn_name = self._pick(ins, ("textnorm",))
        self._logits_out = self._pick(outs, ("ctc_logits", "logits", "output"))
        logger.info(
            "FunASR ORT-DirectML loaded: providers=%s in=%s out=%s vocab=%d",
            self._session.get_providers(),
            ins,
            outs,
            len(self._vocab),
        )

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _providers(compute_units: str) -> list[str]:
        if compute_units == "cpu":
            return ["CPUExecutionProvider"]
        return ["DmlExecutionProvider", "CPUExecutionProvider"]

    @staticmethod
    def _pick(names, candidates) -> str:
        for c in candidates:
            if c in names:
                return c
        return names[0]

    def _build_frontend(self, cfg: dict):
        from funasr.frontends.wav_frontend import WavFrontend

        fe_conf = dict(cfg.get("frontend_conf", _DEFAULT_FRONTEND))
        cmvn = self.model_dir / cfg.get("cmvn_file", "am.mvn")
        fe_conf["cmvn_file"] = str(cmvn)
        fe_conf.setdefault("dither", 0.0)
        fe_conf.setdefault("snip_edges", False)
        return WavFrontend(**fe_conf)

    # -------------------------------------------------------------- inference
    def warmup(self) -> None:
        """Run one zero-input forward pass so the ORT session / GPU is ready."""
        try:
            self._predict(np.zeros(16000, dtype=np.float32))
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("FunASR warmup skipped: %s", exc)

    def _extract_features(self, waveform: np.ndarray) -> tuple[np.ndarray, int]:
        import torch

        if waveform.ndim > 1:
            waveform = waveform.mean(axis=1)
        w = torch.from_numpy(waveform.astype(np.float32)).unsqueeze(0)
        with torch.no_grad():
            feats, flen = self._frontend(w, torch.tensor([w.shape[1]], dtype=torch.int32))
        feats = np.ascontiguousarray(feats.numpy().astype(np.float32))
        return feats, int(feats.shape[1])

    def _predict(self, waveform: np.ndarray, lang_id: int | None = None) -> tuple[np.ndarray, int]:
        feats, tlen = self._extract_features(waveform)  # [1, T, 560]
        lid = self._default_lang if lang_id is None else lang_id
        feeds = {
            self._speech_name: feats,
            self._len_name: np.array([tlen], dtype=np.int32),
            self._lang_name: np.array([lid], dtype=np.int32),
            self._tn_name: np.array([self._default_textnorm], dtype=np.int32),
        }
        logits, lens = self._session.run(
            [
                self._logits_out,
                self._pick([o.name for o in self._session.get_outputs()], ("encoder_out_lens",)),
            ],
            feeds,
        )
        logits = np.asarray(logits)[0]  # [T', V]
        L = int(np.asarray(lens)[0])
        return logits[:L], tlen

    def transcribe(
        self, waveform: np.ndarray, *, language: str | None = None, timeout_s: float | None = None
    ) -> Transcript:
        """Transcribe a 16 kHz mono waveform and return a :class:`Transcript`."""
        t0 = time.perf_counter()
        try:
            lang_id = self._lid.get((language or "auto").lower(), self._default_lang)
            logits, _ = self._predict(waveform, lang_id)
            text = self._ctc_decode(logits)
            return Transcript(
                text=text, engine=self.kind, latency_s=time.perf_counter() - t0, compute_units="dml"
            )
        except Exception as exc:
            return Transcript(
                text="",
                engine=self.kind,
                latency_s=time.perf_counter() - t0,
                compute_units="dml",
                ok=False,
                error=repr(exc),
            )

    # ------------------------------------------------------------------ decode
    def _ctc_decode(self, logits: np.ndarray) -> str:
        """Greedy CTC decode: collapse repeats + blank, then join BPE pieces."""
        if not self._vocab:
            return ""
        ids = np.argmax(logits, axis=-1).tolist()
        collapsed = []
        prev = -1
        for i in ids:
            if i == prev:
                continue
            if i == _BLANK_ID:
                prev = i
                continue
            collapsed.append(i)
            prev = i
        pieces = []
        for i in collapsed:
            tok = self._vocab[i] if 0 <= i < len(self._vocab) else ""
            if tok.startswith("<|") or tok in ("", "<unk>"):
                continue
            pieces.append(tok)
        text = "".join(pieces).replace("▁", " ").strip()
        return text

    def shutdown(self) -> None:
        """Release the ORT session and frontend to free the GPU/resources."""
        self._session = None
        self._frontend = None
