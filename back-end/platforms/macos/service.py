# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""macOS ASR service — replaces the placeholder /transcribe endpoint.

Exposes a stable local API used by the desktop UI / daemon:

  GET  /health                 -> {"ok": true, "engines": [...]}
  POST /transcribe             -> raw audio bytes -> dual-engine fused transcript
  GET  /transcribe/stream      -> SSE: live mic capture, NCE partials + GPU final

Two engines run in parallel per request:
  * FunASR  -> Core ML on the Apple Neural Engine (NCE)
  * Qwen3-ASR -> llama.cpp Metal on the Apple GPU

Run:  python back-end/platforms/macos/service.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from audio import MicCapture, decode_to_16k_mono
from common import EngineKind, resolve_asset
from funasr_coreml import FunASRCoreML
from orchestrator import DualEngineOrchestrator, FusionConfig
from qwen3_gpu import Qwen3GPU

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s %(message)s")
logger = logging.getLogger("voxkey.service")

CONFIG_PATH = Path(os.environ.get("VOXKEY_MACOS_CONFIG", Path(__file__).with_name("config.json")))
HOST = os.environ.get("VOXKEY_ASR_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOXKEY_ASR_PORT", "17863"))

app = FastAPI(title="VoxKey macOS ASR")
STATE: dict = {}

# Allow the desktop shell (Tauri webview) to call the local API and read the SSE
# stream from any origin. Tighten before a public release.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Engine catalogue used for the runtime hot-swap endpoints. ``compute`` mirrors
# the frontend ComputeClass values (npu/gpu).
ENGINE_DEFS = {
    "funasr_coreml": {
        "label": "FunASR CoreML",
        "compute": "npu",
        "model_rel": "models/funasr_coreml/model.onnx",
    },
    "qwen3_gpu": {
        "label": "Qwen3-ASR GPU",
        "compute": "gpu",
        "model_rel": "models/qwen3_asr/qwen3_asr_llm.q4_k.gguf",
    },
}

# Live enablement map, mirrored from config.json and mutated by POST /engines.
ENGINE_CONFIG: dict[str, bool] = {}


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
    if is_on("funasr_coreml"):
        fc = eng.get("funasr_coreml", {})
        funasr = FunASRCoreML(
            str((base / fc["model_path"]).expanduser()),
            compute_units=fc.get("compute_units", "ane"),
            timeout_s=fc.get("timeout_s", 8.0),
        )
        funasr.warmup()

    qwen3 = None
    if is_on("qwen3_gpu"):
        qc = eng.get("qwen3_gpu", {})
        qwen3 = Qwen3GPU(
            model_dir=str((base / qc["model_dir"]).expanduser()),
            project_dir=str(resolve_asset(base, raw["asr_project_dir"]))
            if raw.get("asr_project_dir")
            else None,
            onnx_provider=qc.get("onnx_provider", "CPU"),
            llm_use_gpu=qc.get("llm_use_gpu", True),
            encoder_frontend_fn=qc.get(
                "encoder_frontend_fn", "qwen3_asr_encoder_frontend.int4.onnx"
            ),
            encoder_backend_fn=qc.get("encoder_backend_fn", "qwen3_asr_encoder_backend.int4.onnx"),
            llm_fn=qc.get("llm_fn", "qwen3_asr_llm.q4_k.gguf"),
            enable_aligner=qc.get("enable_aligner", False),
            timeout_s=qc.get("timeout_s", 20.0),
        )
        qwen3.warmup()

    if funasr is None and qwen3 is None:
        raise RuntimeError("No ASR engine enabled")

    orch = DualEngineOrchestrator(
        funasr=funasr,
        qwen3=qwen3,
        fusion=FusionConfig(
            mode=fusion.get("mode", "fast_first"),
            funasr_priority_until_s=fusion.get("funasr_priority_until_s", 1.2),
            min_agreement_chars=fusion.get("min_agreement_chars", 4),
        ),
    )
    logger.info("Engines loaded: funasr=%s qwen3=%s", funasr is not None, qwen3 is not None)
    return orch


def build_engines_payload() -> list[dict]:
    """Snapshot of every engine's present/enabled/loaded status for the UI."""
    orch = STATE.get("orch")
    payload: list[dict] = []
    for engine_id, meta in ENGINE_DEFS.items():
        loaded = False
        if orch:
            if engine_id == "funasr_coreml":
                loaded = orch.funasr is not None
            elif engine_id == "qwen3_gpu":
                loaded = orch.qwen3 is not None
        model_path = CONFIG_PATH.parent / meta["model_rel"]
        payload.append(
            {
                "id": engine_id,
                "label": meta["label"],
                "compute": meta["compute"],
                "present": model_path.exists(),
                "enabled": ENGINE_CONFIG.get(engine_id, True),
                "loaded": loaded,
                "path": str(model_path) if model_path.exists() else None,
                "size_bytes": model_path.stat().st_size if model_path.exists() else None,
            }
        )
    return payload


def persist_engine_config() -> None:
    """Write the current enablement map back to config.json (survives restart)."""
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    engines = raw.setdefault("engines", {})
    for engine_id in ENGINE_DEFS:
        engines.setdefault(engine_id, {})["enabled"] = ENGINE_CONFIG.get(engine_id, True)
    CONFIG_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


@app.on_event("startup")
def _startup() -> None:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    eng = raw.get("engines", {})
    ENGINE_CONFIG["funasr_coreml"] = eng.get("funasr_coreml", {}).get("enabled", True)
    ENGINE_CONFIG["qwen3_gpu"] = eng.get("qwen3_gpu", {}).get("enabled", True)
    STATE["orch"] = load_engines(ENGINE_CONFIG)


@app.on_event("shutdown")
def _shutdown() -> None:
    orch: DualEngineOrchestrator | None = STATE.get("orch")
    if orch:
        if orch.funasr:
            orch.funasr.shutdown()
        if orch.qwen3:
            orch.qwen3.shutdown()


@app.get("/health")
def health() -> JSONResponse:
    """Report liveness and which engines are currently loaded."""
    orch: DualEngineOrchestrator | None = STATE.get("orch")
    engines = []
    if orch and orch.funasr:
        engines.append({"kind": EngineKind.FUNASR_NCE.value, "compute": "ane"})
    if orch and orch.qwen3:
        engines.append({"kind": EngineKind.QWEN3_GPU.value, "compute": "gpu"})
    return JSONResponse({"ok": True, "service": "voxkey-asr-macos", "engines": engines})


@app.get("/engines")
def engines() -> JSONResponse:
    """Report each engine's file presence, config enablement and runtime load."""
    return JSONResponse(build_engines_payload())


@app.post("/engines")
async def set_engines(request: Request) -> JSONResponse:
    """Hot-swap engines.

    Builds the new orchestrator *before* touching disk or in-memory state, so a
    failed rebuild (e.g. both engines disabled) leaves the running service
    untouched. The previous engines are shut down only after the swap, releasing
    their GPU/ANE resources.
    """
    body = await request.json()
    enabled = body.get("enabled", {})
    new_config = dict(ENGINE_CONFIG)
    for engine_id in ENGINE_DEFS:
        if engine_id in enabled:
            new_config[engine_id] = bool(enabled[engine_id])

    if not any(new_config.values()):
        return JSONResponse(
            {"error": "at least one engine must remain enabled"}, status_code=400
        )

    try:
        new_orch = load_engines(new_config)
    except RuntimeError as exc:
        return JSONResponse({"error": f"engine rebuild failed: {exc}"}, status_code=500)

    old = STATE.get("orch")
    STATE["orch"] = new_orch
    ENGINE_CONFIG.clear()
    ENGINE_CONFIG.update(new_config)
    persist_engine_config()

    if old:
        if old.funasr:
            old.funasr.shutdown()
        if old.qwen3:
            old.qwen3.shutdown()
    return JSONResponse(build_engines_payload())


@app.post("/transcribe")
async def transcribe(request: Request) -> JSONResponse:
    """Accept raw audio bytes, decode to 16 kHz mono, and return the fused transcript."""
    orch: DualEngineOrchestrator = STATE["orch"]
    data = await request.body()
    try:
        waveform = decode_to_16k_mono(data)
    except Exception as exc:
        return JSONResponse(
            {"text": "", "status": "decode_error", "error": repr(exc)}, status_code=422
        )
    lang = request.query_params.get("language")
    result = orch.transcribe(waveform, language=lang)
    return JSONResponse(result.to_json())


@app.get("/transcribe/stream")
async def transcribe_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events: live mic capture -> NCE partials, then GPU final.

    Query params: max_seconds (default 30), emit_interval (default 0.8).
    The client disconnects to stop recording.
    """
    orch: DualEngineOrchestrator = STATE["orch"]
    max_seconds = float(request.query_params.get("max_seconds", "30"))
    emit_interval = float(request.query_params.get("emit_interval", "0.8"))

    async def event_gen():
        cap = MicCapture()
        cap.start()
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()
        stop = {"t": False}

        def producer():
            try:
                for chunk in cap.iter_chunks(chunk_seconds=emit_interval):
                    if stop["t"]:
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        import threading

        th = threading.Thread(target=producer, daemon=True)
        th.start()

        buf = []
        deadline = time.perf_counter() + max_seconds
        yield _sse("start", {})
        try:
            while True:
                if await request.is_disconnected():
                    break
                if time.perf_counter() > deadline:
                    break
                chunk = await queue.get()
                if chunk is None:
                    break
                buf.append(chunk)
                partial = np.concatenate(buf)
                # low-latency NCE partial
                if orch.funasr and len(partial) > 16_000 * 0.4:
                    tr = orch.funasr.transcribe(partial)
                    if tr.text.strip():
                        yield _sse("partial", {"text": tr.text, "compute": "ane"})
        finally:
            cap.stop()
            stop["t"] = True

        full = np.concatenate(buf) if buf else np.zeros(0, dtype=np.float32)
        if len(full) > 0:
            result = orch.transcribe(full)
            yield _sse("final", result.to_json())
        yield _sse("end", {})

    return StreamingResponse(event_gen(), media_type="text/event-stream")


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
