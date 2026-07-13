# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Windows ASR service — dual-engine (FunASR DirectML + Qwen3-ASR Vulkan).

Exposes a stable local API used by the desktop UI / daemon:

  GET  /health      -> {"ok": true, "engines": [...]}
  POST /transcribe  -> raw audio bytes -> dual-engine fused transcript
  GET  /engines     -> engine presence / enablement / load state
  POST /engines     -> hot-swap engines (persist to config.json, rebuild)

This is the Windows port of back-end/platforms/macos/service.py. The daemon
(windows_daemon.py) POSTs raw audio to /transcribe and reads {"text": "..."},
so the HTTP contract is kept identical for zero-regression integration.

Run:  python back-end/platforms/windows/service.py
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from common import EngineKind, resolve_asset
from funasr_directml import FunASRDirectML
from orchestrator import DualEngineOrchestrator, FusionConfig
from qwen3_gpu import Qwen3GPU

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s %(message)s")
logger = logging.getLogger("voxkey.service")

CONFIG_PATH = Path(os.environ.get("VOXKEY_WIN_CONFIG", Path(__file__).with_name("config.json")))
HOST = os.environ.get("VOXKEY_ASR_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOXKEY_ASR_PORT", "17863"))

STATE: dict = {}

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: "FastAPI"):  # noqa: ARG001
    _startup()
    try:
        yield
    finally:
        _shutdown()


app = FastAPI(title="VoxKey Windows ASR", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Engine catalogue used for the runtime hot-swap endpoints. ``compute`` mirrors
# the frontend ComputeClass values (dml/gpu).
ENGINE_DEFS = {
    "funasr_directml": {
        "label": "FunASR DirectML",
        "compute": "dml",
        "model_rel": "models/funasr_directml/model.onnx",
    },
    "qwen3_gpu": {
        "label": "Qwen3-ASR GPU",
        "compute": "gpu",
        "model_rel": "models/qwen3_asr/qwen3_asr_llm.q4_k.gguf",
    },
}

ENGINE_CONFIG: dict[str, bool] = {}


def _engine_uses_gpu(engine_id: str, cfg: dict) -> bool:
    """该引擎启用后是否会占用共享显存 GPU。

    qwen3_gpu 永远走 Vulkan；funasr_directml 仅当 compute_units=='dml' 时占 GPU
    （cpu 模式可与 qwen3 安全共存）。
    """
    if engine_id == "qwen3_gpu":
        return True
    if engine_id == "funasr_directml":
        fc = (cfg.get("engines", {}) or {}).get("funasr_directml", {}) or {}
        return fc.get("compute_units", "dml") == "dml"
    return False


def _engine_warnings(enabled: dict, cfg: dict) -> list[str]:
    """双 GPU 引擎（Qwen3/Vulkan + FunASR/DirectML）同时启用时的提示。

    不做互斥——高端独立显卡可以双跑。仅在共享显存设备（如 Radeon 780M）上会
    因显存压力触发 GPU 设备重置、转写失败，因此提示用户需要更高级的显卡，或把
    FunASR 的 compute_units 改为 cpu 以错开 GPU 占用。
    """
    w: list[str] = []
    qwen_gpu = _engine_uses_gpu("qwen3_gpu", cfg) and enabled.get("qwen3_gpu", False)
    fun_gpu = _engine_uses_gpu("funasr_directml", cfg) and enabled.get("funasr_directml", False)
    if qwen_gpu and fun_gpu:
        w.append(
            "同时启用 Qwen3(Vulkan) 与 FunASR(DirectML) 两个 GPU 引擎需要独立高端显卡；"
            "在集成显卡/共享显存设备上会触发 GPU 设备重置、转写失败。建议仅启用其中一个，"
            "或把 FunASR 的 compute_units 设为 cpu。"
        )
    return w


def _current_warnings() -> list[str]:
    """基于当前生效的引擎开关计算警告。"""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    engines = (raw.get("engines", {}) or {})
    # engines 段每个值是引擎配置 dict（含 enabled 字段），提取为 {id: bool}。
    enabled = {k: bool((v or {}).get("enabled", False)) for k, v in engines.items()}
    return _engine_warnings(enabled, raw)


def _decode_to_16k_mono(data: bytes) -> np.ndarray:
    """Decode raw audio bytes to a float32 16 kHz mono waveform in [-1, 1]."""
    # Fast path: ffmpeg (resamples + downmix in one step) if available.
    ffmpeg = os.environ.get("VOXKEY_FFMPEG", "ffmpeg")
    if shutil_which(ffmpeg):
        return _decode_via_ffmpeg(data, ffmpeg)
    # Fallback: decode with soundfile, then resample with scipy if needed.
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as src:
        src.write(data)
        src_path = src.name
    try:
        waveform, rate = sf.read(src_path, dtype="float32", always_2d=False)
    finally:
        Path(src_path).unlink(missing_ok=True)
    if waveform.ndim > 1:
        waveform = waveform.mean(axis=1)
    if rate != 16_000:
        waveform = _resample(waveform, rate, 16_000)
    return waveform.astype(np.float32)


def _decode_via_ffmpeg(data: bytes, ffmpeg: str) -> np.ndarray:
    import subprocess

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as src:
        src.write(data)
        src_path = src.name
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as dst:
        dst_path = dst.name
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src_path,
             "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", "-f", "wav", dst_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode().strip()[-300:]}")
        waveform, rate = sf.read(dst_path, dtype="float32", always_2d=False)
        if waveform.ndim > 1:
            waveform = waveform.mean(axis=1)
        return waveform.astype(np.float32)
    finally:
        Path(src_path).unlink(missing_ok=True)
        Path(dst_path).unlink(missing_ok=True)


def _resample(waveform: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    try:
        from scipy.signal import resample

        n = int(round(len(waveform) * dst_rate / src_rate))
        return resample(waveform, n).astype(np.float32)
    except Exception as exc:  # pragma: no cover
        logger.warning("Resample unavailable (%s); returning as-is", exc)
        return waveform.astype(np.float32)


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def load_engines(enabled_override: dict[str, bool] | None = None) -> DualEngineOrchestrator:
    """Build the dual-engine orchestrator, honouring ``enabled_override`` when set."""
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    base = CONFIG_PATH.parent
    eng = raw.get("engines", {})
    fusion = raw.get("fusion", {})

    def is_on(engine_id: str) -> bool:
        if enabled_override is not None:
            return enabled_override.get(engine_id, True)
        return eng.get(engine_id, {}).get("enabled", True)

    funasr = None
    if is_on("funasr_directml"):
        try:
            fc = eng.get("funasr_directml", {})
            model_path = fc.get("model_path", "models/funasr_directml")
            funasr = FunASRDirectML(
                str((base / model_path).expanduser()) if not Path(model_path).is_absolute()
                else model_path,
                compute_units=fc.get("compute_units", "dml"),
                timeout_s=fc.get("timeout_s", 8.0),
            )
            funasr.warmup()
        except Exception as exc:  # noqa: BLE001
            logger.warning("FunASR 引擎加载失败，已跳过：%s", exc)
            funasr = None

    qwen3 = None
    if is_on("qwen3_gpu"):
        try:
            qc = eng.get("qwen3_gpu", {})
            model_dir = qc.get("model_dir", "models/qwen3_asr")
            qwen3 = Qwen3GPU(
                model_dir=str((base / model_dir).expanduser()) if not Path(model_dir).is_absolute()
                else model_dir,
                project_dir=str(resolve_asset(base, raw["asr_project_dir"]))
                if raw.get("asr_project_dir")
                else None,
                onnx_provider=qc.get("onnx_provider", "CPU"),
                llm_use_gpu=qc.get("llm_use_gpu", True),
                encoder_frontend_fn=qc.get("encoder_frontend_fn", "qwen3_asr_encoder_frontend.int4.onnx"),
                encoder_backend_fn=qc.get("encoder_backend_fn", "qwen3_asr_encoder_backend.int4.onnx"),
                llm_fn=qc.get("llm_fn", "qwen3_asr_llm.q4_k_m.gguf"),
                enable_aligner=qc.get("enable_aligner", False),
                timeout_s=qc.get("timeout_s", 20.0),
            )
            qwen3.warmup()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Qwen3 引擎加载失败，已跳过：%s", exc)
            qwen3 = None

    if funasr is None and qwen3 is None:
        logger.warning(
            "两个引擎均未能加载（模型/DLL 缺失？）。服务仍会启动，/health 将显示空引擎列表；"
            "放好资源后通过 POST /engines 重新加载。"
        )
    return DualEngineOrchestrator(
        funasr=funasr,
        qwen3=qwen3,
        fusion=FusionConfig(
            mode=fusion.get("mode", "fast_first"),
            funasr_priority_until_s=fusion.get("funasr_priority_until_s", 1.2),
            min_agreement_chars=fusion.get("min_agreement_chars", 4),
        ),
    )


def build_engines_payload() -> list[dict]:
    orch = STATE.get("orch")
    payload: list[dict] = []
    for engine_id, meta in ENGINE_DEFS.items():
        loaded = False
        if orch:
            if engine_id == "funasr_directml":
                loaded = orch.funasr is not None
            elif engine_id == "qwen3_gpu":
                loaded = orch.qwen3 is not None
        model_path = CONFIG_PATH.parent / meta["model_rel"]
        payload.append({
            "id": engine_id,
            "label": meta["label"],
            "compute": meta["compute"],
            "present": model_path.exists(),
            "enabled": ENGINE_CONFIG.get(engine_id, True),
            "loaded": loaded,
            "path": str(model_path) if model_path.exists() else None,
            "size_bytes": model_path.stat().st_size if model_path.exists() else None,
        })
    return payload


def persist_engine_config() -> None:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    engines = raw.setdefault("engines", {})
    for engine_id in ENGINE_DEFS:
        engines.setdefault(engine_id, {})["enabled"] = ENGINE_CONFIG.get(engine_id, True)
    CONFIG_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _startup() -> None:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    eng = raw.get("engines", {})
    ENGINE_CONFIG["funasr_directml"] = eng.get("funasr_directml", {}).get("enabled", True)
    ENGINE_CONFIG["qwen3_gpu"] = eng.get("qwen3_gpu", {}).get("enabled", True)
    STATE["orch"] = load_engines(ENGINE_CONFIG)


def _shutdown() -> None:
    orch: DualEngineOrchestrator | None = STATE.get("orch")
    if orch:
        if orch.funasr:
            orch.funasr.shutdown()
        if orch.qwen3:
            orch.qwen3.shutdown()


@app.get("/health")
def health() -> JSONResponse:
    orch: DualEngineOrchestrator | None = STATE.get("orch")
    engines = []
    if orch and orch.funasr:
        engines.append({"kind": EngineKind.FUNASR_DML.value, "compute": "dml"})
    if orch and orch.qwen3:
        engines.append({"kind": EngineKind.QWEN3_GPU.value, "compute": "gpu"})
    return JSONResponse({
        "ok": True,
        "service": "voxkey-asr-win",
        "engines": engines,
        "warnings": _current_warnings(),
    })


@app.get("/engines")
def engines() -> JSONResponse:
    return JSONResponse({"engines": build_engines_payload(), "warnings": _current_warnings()})


@app.post("/engines")
async def set_engines(request: Request) -> JSONResponse:
    body = await request.json()
    enabled = body.get("enabled", {})
    for engine_id in ENGINE_DEFS:
        if engine_id in enabled:
            ENGINE_CONFIG[engine_id] = bool(enabled[engine_id])
    persist_engine_config()
    old = STATE.get("orch")
    try:
        STATE["orch"] = load_engines(ENGINE_CONFIG)
    except RuntimeError as exc:
        logger.warning("Engine rebuild left no engine loaded: %s", exc)
    # 释放被替换掉的旧编排器（否则旧的 Vulkan/DirectML 会话会随反复调用累积泄漏）。
    if old is not None and old is not STATE.get("orch"):
        try:
            if old.funasr:
                old.funasr.shutdown()
            if old.qwen3:
                old.qwen3.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.warning("释放旧引擎会话时出错（忽略）：%s", exc)
    return JSONResponse({"engines": build_engines_payload(), "warnings": _current_warnings()})


@app.post("/transcribe")
async def transcribe(request: Request) -> JSONResponse:
    orch: DualEngineOrchestrator = STATE["orch"]
    data = await request.body()
    try:
        waveform = _decode_to_16k_mono(data)
    except Exception as exc:
        return JSONResponse(
            {"text": "", "status": "decode_error", "error": repr(exc)}, status_code=422
        )
    lang = request.query_params.get("language")
    result = orch.transcribe(waveform, language=lang)
    return JSONResponse(result.to_json())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
