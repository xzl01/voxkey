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
import functools
import hmac
import json
import logging
import math
import os
import queue as _queue
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, StrictBool

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

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    try:
        await _startup()
        yield
    finally:
        await _shutdown()


app = FastAPI(title="VoxKey macOS ASR", lifespan=_lifespan)
STATE: dict = {}

# --- Local auth ----------------------------------------------------------- #
# Every state-changing / side-effecting endpoint (starting the mic, loading
# models, transcribing a file) requires a per-process token. The token is
# generated at startup and written to a file the desktop shell (Rust) can read
# and hand to the webview; it is *not* derivable by an arbitrary web page, so a
# malicious site cannot start the mic or thrash model reloads via localhost.
def _token_file_path() -> Path:
    default = Path.home() / "Library" / "Caches" / "dev.xzl01.voxkey" / "asr_token"
    path = Path(os.environ.get("VOXKEY_ASR_TOKEN_FILE", str(default))).expanduser()
    if not path.is_absolute():
        raise ValueError("VOXKEY_ASR_TOKEN_FILE must be an absolute path")
    return path


def _gen_token() -> str:
    return secrets.token_hex(16)


def _publish_token(token: str) -> Path:
    """Atomically publish ``token`` at the one path shared with the desktop.

    Multiple fallback files are intentionally avoided: if the primary becomes
    unreadable/unwritable, the shell cannot reliably know which candidate is
    current. A publication failure therefore aborts service startup instead of
    leaving a healthy-looking process that can only return 401.
    """
    target = _token_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd: int | None = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None  # fdopen owns and closes it
            f.write(token)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        target.chmod(0o600)
        return target
    except OSError:
        if fd is not None:
            os.close(fd)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_provided_token(request: Request) -> str | None:
    # Never accept credentials in the URL: query strings leak into logs,
    # history and diagnostics. The desktop uses this dedicated header.
    return request.headers.get("X-VoxKey-Token")


def _require_token(request: Request) -> None:
    """Raise 401 unless the request carries the live process token."""
    expected = STATE.get("token")
    if not expected:
        raise HTTPException(status_code=503, detail="ASR auth token is not initialized")
    provided = _read_provided_token(request)
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid or missing ASR auth token")


# --- CORS ---------------------------------------------------------------- #
# Restrict to the desktop shell's own origins (Tauri webview + Vite dev
# server). An arbitrary web page is not an allowed origin, so even if it could
# guess the token it cannot read the responses; and without the token the
# side-effecting endpoints reject it outright.
DEFAULT_CORS_ORIGINS = [
    "tauri://localhost",
    "https://tauri.localhost",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
]
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("VOXKEY_ASR_CORS_ORIGINS", "").split(",")
    if o.strip()
] or DEFAULT_CORS_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*", "X-VoxKey-Token"],
    allow_credentials=False,
)


# Serialize hot-swaps so only one rebuild/drain runs at a time.
SWAP_LOCK = asyncio.Lock()


class _HandleDraining(Exception):
    """Raised by ``EngineHandle.lease`` when the handle is mid hot-swap.

    Callers catch this and re-acquire the *current* handle instead of blocking
    forever on a handle whose drain already completed (a request could grab the
    old handle, a swap marks it draining + finishes, and the request would then
    wait on a notify that never comes).
    """


class _ServiceNotReady(Exception):
    """Raised by ``_lease_current`` when no orchestrator is live yet."""


class EngineHandle:
    """Wrap the live orchestrator with a lease/ref-count so a hot-swap can
    drain in-flight requests *before* freeing engine resources.

    A request takes a lease (``async with handle.lease()``). ``drain()`` flips a
    draining flag and waits for every lease to release before calling
    ``shutdown()``, so an in-flight transcription never has its model torn down
    underneath it. If ``drain`` times out because a request is still running, it
    returns ``False`` and defers shutdown rather than force-releasing an active
    engine; reclamation is then retried in the background until the work ends.
    """

    def __init__(self, orch: DualEngineOrchestrator) -> None:
        self.orch = orch
        self._refs = 0
        self._draining = False
        self._shut = False
        # Engine object ids the *new* orchestrator reuses; we must NOT shut
        # these down when this (old) handle is drained, or we'd kill an engine
        # the live service still depends on.
        self._keep_ids: set[int] = set()
        self._cond = asyncio.Condition()

    @asynccontextmanager
    async def lease(self):
        async with self._cond:
            # Refuse new leases once a swap has started; the caller re-fetches
            # the current handle instead of blocking on a draining one that may
            # already have finished draining.
            if self._draining:
                raise _HandleDraining
            self._refs += 1
        try:
            yield self.orch
        finally:
            async with self._cond:
                self._refs -= 1
                if self._refs <= 0 and self._draining:
                    self._cond.notify_all()

    async def drain(self, timeout: float = 30.0) -> bool:
        """Wait up to ``timeout`` for in-flight leases to clear, then shut the
        engine down. Returns ``True`` if drained, ``False`` if it timed out with
        active leases (so the caller can defer reclamation instead of killing a
        live request). ``timeout=None`` waits indefinitely.
        """
        loop = asyncio.get_event_loop()
        async with self._cond:
            self._draining = True
            try:
                await asyncio.wait_for(
                    self._cond.wait_for(lambda: self._refs <= 0), timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "hot-swap drain timed out with %d in-flight lease(s); "
                    "deferring shutdown rather than interrupting active work",
                    self._refs,
                )
                return False
        await loop.run_in_executor(None, self._shutdown)
        return True

    def _shutdown(self) -> None:
        if self._shut:
            return
        self._shut = True
        if self.orch.funasr is not None and id(self.orch.funasr) not in self._keep_ids:
            self.orch.funasr.shutdown()
        if self.orch.qwen3 is not None and id(self.orch.qwen3) not in self._keep_ids:
            self.orch.qwen3.shutdown()


@asynccontextmanager
async def _lease_current():
    """Acquire a lease on the *current* orchestrator, retrying across a
    concurrent hot-swap. Closes the race where a request fetches the old handle
    and a swap drains it: the lease is refused and we simply grab the new one.
    """
    while True:
        handle = STATE.get("orch")
        if handle is None:
            raise _ServiceNotReady
        try:
            async with handle.lease() as orch:
                yield orch
                return
        except _HandleDraining:
            await asyncio.sleep(0)
            continue


async def _reclaim(handle: EngineHandle) -> None:
    """Best-effort deferred reclamation for a hot-swapped engine whose drain
    timed out: wait (without deadline) for in-flight work to finish, then shut
    it down. The active engine is never torn down mid-request.
    """
    try:
        await handle.drain(timeout=None)
    except Exception:  # noqa: BLE001
        logger.exception("deferred engine reclamation failed")


# Streaming parameter bounds (reject 0 / negative / NaN / inf / absurd).
EMIT_MIN, EMIT_MAX = 0.05, 5.0
MAXSEC_MIN, MAXSEC_MAX = 1.0, 120.0
# Bounded hand-off queue between the capture thread and the event loop, so a
# slow consumer applies backpressure instead of letting memory grow.
MAX_QUEUE = 8

# Hard cap on uploaded audio so a client (or a malicious page) cannot exhaust
# memory/disk by POSTing an unbounded body. Checked via Content-Length first,
# then enforced by a streaming bounded read.
MAX_BODY_BYTES = 50 * 1024 * 1024  # 50 MiB

# Partial transcripts use a sliding window + throttled emission so cost stays
# bounded (O(window) per emit, not O(recording length)). The FINAL transcript
# always uses the complete captured audio, independent of this window.
PARTIAL_WINDOW_SEC = 8.0
PARTIAL_MAX_SAMPLES = int(16_000 * PARTIAL_WINDOW_SEC)
PARTIAL_MIN_INTERVAL = 0.5  # seconds between partial emissions


def _clamp(value, lo: float, hi: float, default: float) -> float:
    """Clamp a query-param value to ``[lo, hi]``; non-finite/garbage -> default."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return max(lo, min(hi, f))

# NOTE: the CORS policy is registered ONCE, above (restricted to the desktop
# shell's own origins). Do not add a second `allow_origins=["*"]` middleware
# here — a second CORSMiddleware would override the restricted one and reopen
# the local service to any web page.

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


class _LockedEngine:
    """Serialize every call to one physical engine.

    A single FunASR/Qwen object is not internally reentrant: file
    transcription, SSE partials and the final pass can all land on the same
    engine concurrently, which can crash it or corrupt inference state. We wrap
    each engine in a mutex so at most one transcription runs on it at a time.
    Attribute access is forwarded to the real engine for everything else.
    """

    def __init__(self, engine, lock: threading.Lock | None = None) -> None:
        self._engine = engine
        self._lock = lock or threading.Lock()

    def transcribe(self, waveform, *, language=None, timeout_s=None):
        try:
            limit = float(timeout_s if timeout_s is not None else self.timeout_s)
        except (TypeError, ValueError):
            limit = float(self.timeout_s)
        if not math.isfinite(limit) or limit <= 0:
            raise TimeoutError("engine timeout must be a positive finite value")

        started = time.perf_counter()
        if not self._lock.acquire(timeout=limit):
            raise TimeoutError(f"engine queue wait exceeded {limit:.1f}s")
        try:
            remaining = max(0.001, limit - (time.perf_counter() - started))
            return self._engine.transcribe(
                waveform,
                language=language,
                timeout_s=remaining,
            )
        finally:
            self._lock.release()

    def shutdown(self):
        with self._lock:
            self._engine.shutdown()

    @property
    def timeout_s(self):
        return self._engine.timeout_s

    def __getattr__(self, name):
        return getattr(self._engine, name)


def load_engines(
    enabled_override: dict[str, bool] | None = None,
    reuse_from: DualEngineOrchestrator | None = None,
) -> DualEngineOrchestrator:
    """Build the dual-engine orchestrator, honouring ``enabled_override`` when set.

    ``reuse_from`` lets a hot-swap reuse engines that are still enabled instead
    of reloading every model: toggling one engine no longer re-warms the other,
    avoiding a transient second copy of the ANE/GPU model in memory.
    """
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    base = CONFIG_PATH.parent
    eng = raw.get("engines", {})
    fusion = raw.get("fusion", {})

    def is_on(engine_id: str) -> bool:
        if enabled_override is not None:
            return enabled_override.get(engine_id, True)
        return eng.get(engine_id, {}).get("enabled", True)

    def reuse(kind: str):
        if reuse_from is None:
            return None
        return reuse_from.funasr if kind == "funasr" else reuse_from.qwen3

    funasr = None
    if is_on("funasr_coreml"):
        if reuse_from is not None and reuse_from.funasr is not None:
            funasr = reuse_from.funasr  # already loaded & still enabled -> reuse
        else:
            fc = eng.get("funasr_coreml", {})
            funasr = _LockedEngine(
                FunASRCoreML(
                    str((base / fc["model_path"]).expanduser()),
                    compute_units=fc.get("compute_units", "ane"),
                    timeout_s=fc.get("timeout_s", 8.0),
                )
            )
            funasr._engine.warmup()

    qwen3 = None
    if is_on("qwen3_gpu"):
        if reuse_from is not None and reuse_from.qwen3 is not None:
            qwen3 = reuse_from.qwen3  # already loaded & still enabled -> reuse
        else:
            qc = eng.get("qwen3_gpu", {})
            qwen3 = _LockedEngine(
                Qwen3GPU(
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
            )
            qwen3._engine.warmup()

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


def _shutdown_orch(orch: DualEngineOrchestrator, keep_ids: set[int] | None = None) -> None:
    """Shut down an orchestrator's engines, skipping any in ``keep_ids`` (engines
    shared with another live handle that still owns them)."""
    keep_ids = keep_ids or set()
    if orch.funasr is not None and id(orch.funasr) not in keep_ids:
        orch.funasr.shutdown()
    if orch.qwen3 is not None and id(orch.qwen3) not in keep_ids:
        orch.qwen3.shutdown()



def build_engines_payload() -> list[dict]:
    """Snapshot of every engine's present/enabled/loaded status for the UI."""
    handle = STATE.get("orch")
    orch = handle.orch if handle else None
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


def persist_engine_config(new_config: dict[str, bool]) -> None:
    """Atomically persist ``new_config`` to config.json (survives restart).

    Writes a temp file beside the config and ``os.replace``-es it into place so
    a crash mid-write can never leave a half-written config behind. The caller
    must persist BEFORE swapping the in-memory state, so disk and memory stay
    consistent.
    """
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    engines = raw.setdefault("engines", {})
    for engine_id in ENGINE_DEFS:
        engines.setdefault(engine_id, {})["enabled"] = new_config.get(engine_id, True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp, CONFIG_PATH)


async def _startup() -> None:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    eng = raw.get("engines", {})
    ENGINE_CONFIG["funasr_coreml"] = eng.get("funasr_coreml", {}).get("enabled", True)
    ENGINE_CONFIG["qwen3_gpu"] = eng.get("qwen3_gpu", {}).get("enabled", True)
    # Generate (or adopt) the per-process auth token and publish it where the
    # desktop shell can read it. A malicious web page cannot obtain it, so it
    # cannot drive the mic / model endpoints. The in-memory token is
    # authoritative; the file is only the hand-off channel. If the primary path
    # is unwritable we fall back beside config.json (same writable dir) so a
    # write error never leaves the service unreachable.
    token = os.environ.get("VOXKEY_ASR_TOKEN") or _gen_token()
    try:
        written = _publish_token(token)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"could not publish ASR auth token: {exc}") from exc
    STATE["token"] = token
    logger.info("ASR auth token published at %s", written)
    STATE["token_file"] = written
    loop = asyncio.get_event_loop()
    # Build + warmup off the event loop so startup never blocks requests.
    orch = await loop.run_in_executor(None, load_engines, ENGINE_CONFIG)
    STATE["orch"] = EngineHandle(orch)


async def _shutdown() -> None:
    handle: EngineHandle | None = STATE.pop("orch", None)
    if handle:
        await handle.drain(timeout=10)
    token_file: Path | None = STATE.get("token_file")
    token = STATE.get("token")
    if token_file is not None:
        try:
            # Do not remove a token another process may have replaced.
            if token_file.read_text(encoding="utf-8") == token:
                token_file.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove ASR auth token file at %s", token_file)
    STATE.pop("token", None)
    STATE.pop("token_file", None)


@app.get("/health")
def health() -> JSONResponse:
    """Report liveness and which engines are currently loaded."""
    handle = STATE.get("orch")
    orch = handle.orch if handle else None
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


class EngineEnableRequest(BaseModel):
    """Strict request body for ``POST /engines``.

    ``enabled`` maps engine id -> a *real* bool. ``StrictBool`` rejects
    strings/numbers (e.g. ``"false"`` would otherwise be coerced to ``True``
    by ``bool()``), and a non-object / null body is rejected by FastAPI with
    422 instead of producing a 500.
    """

    enabled: dict[str, StrictBool]


@app.post("/engines")
async def set_engines(req: Request, payload: EngineEnableRequest) -> JSONResponse:
    """Hot-swap engines.

    Serialized by ``SWAP_LOCK`` so two swaps cannot race. Unchanged engines are
    reused (no reload), and the config is persisted to disk *before* the
    in-memory state is swapped, so a write failure leaves the service exactly
    as it was. The previous engines are shut down only after the swap, and only
    once every in-flight request that leased them has finished (``drain``).
    """
    _require_token(req)
    async with SWAP_LOCK:
        enabled = payload.enabled
        unknown = sorted(set(enabled) - set(ENGINE_DEFS))
        if unknown:
            return JSONResponse(
                {"error": f"unknown engine id(s): {', '.join(unknown)}"},
                status_code=422,
            )
        if not enabled:
            return JSONResponse(build_engines_payload())

        new_config = dict(ENGINE_CONFIG)
        for engine_id in ENGINE_DEFS:
            if engine_id in enabled:
                new_config[engine_id] = bool(enabled[engine_id])

        if new_config == ENGINE_CONFIG:
            return JSONResponse(build_engines_payload())

        if not any(new_config.values()):
            return JSONResponse(
                {"error": "at least one engine must remain enabled"}, status_code=400
            )

        loop = asyncio.get_event_loop()
        old = STATE.get("orch")
        old_orch = old.orch if old else None
        try:
            # Reuse still-enabled engines from the live orchestrator so we don't
            # re-warm models that are merely staying on.
            new_orch = await loop.run_in_executor(
                None, functools.partial(load_engines, new_config, old_orch)
            )
        except RuntimeError as exc:
            return JSONResponse({"error": f"engine rebuild failed: {exc}"}, status_code=500)

        # Persist FIRST, atomically. If the disk write fails we discard the
        # freshly built engines and keep the old in-memory state intact.
        try:
            persist_engine_config(new_config)
        except OSError as exc:
            keep = {id(e) for e in (old_orch.funasr, old_orch.qwen3) if e}
            await loop.run_in_executor(None, _shutdown_orch, new_orch, keep)
            return JSONResponse(
                {"error": f"failed to persist engine config: {exc}"}, status_code=500
            )

        # Commit in-memory state only after the disk write succeeded.
        STATE["orch"] = EngineHandle(new_orch)
        ENGINE_CONFIG.clear()
        ENGINE_CONFIG.update(new_config)

        if old:
            # Engines the new orchestrator reused must survive the old handle's
            # drain; mark them so _shutdown skips them.
            old._keep_ids = {id(e) for e in (new_orch.funasr, new_orch.qwen3) if e}
            # Blocks (async) until in-flight leases release, then shuts down.
            # If a long transcription is still running past the timeout we must
            # NOT tear the engine down under it, so drain returns False and we
            # defer reclamation to a background task that retries until idle.
            done = await old.drain(timeout=30)
            if not done:
                asyncio.ensure_future(_reclaim(old))
    return JSONResponse(build_engines_payload())


@app.post("/transcribe")
async def transcribe(request: Request) -> JSONResponse:
    """Accept raw audio bytes, decode to 16 kHz mono, and return the fused transcript."""
    _require_token(request)
    if STATE.get("orch") is None:
        return JSONResponse({"error": "service not ready"}, status_code=503)
    # Reject oversized bodies up front via Content-Length, then read the stream
    # into a bounded buffer so a client cannot exhaust memory with a huge body.
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > MAX_BODY_BYTES:
                return JSONResponse(
                    {"error": f"payload too large (> {MAX_BODY_BYTES} bytes)"},
                    status_code=413,
                )
        except ValueError:
            pass
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > MAX_BODY_BYTES:
            return JSONResponse(
                {"error": f"payload too large (> {MAX_BODY_BYTES} bytes)"},
                status_code=413,
            )
    data = bytes(buf)
    try:
        waveform = decode_to_16k_mono(data)
    except Exception as exc:
        return JSONResponse(
            {"text": "", "status": "decode_error", "error": repr(exc)}, status_code=422
        )
    lang = request.query_params.get("language")
    loop = asyncio.get_event_loop()
    try:
        # Lease the *current* orchestrator: a concurrent hot-swap that starts
        # mid-flight is retried transparently, so a swap can never tear the
        # model down under this inference. Blocking work runs off the loop.
        async with _lease_current() as orch:
            fn = functools.partial(orch.transcribe, waveform, language=lang)
            result = await loop.run_in_executor(None, fn)
    except RuntimeError as exc:
        return JSONResponse({"error": f"transcription failed: {exc}"}, status_code=500)
    return JSONResponse(result.to_json())


@app.get("/transcribe/stream")
async def transcribe_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events: live mic capture -> NCE partials, then GPU final.

    Query params: max_seconds (default 30), emit_interval (default 0.8),
    both clamped to sane finite bounds. The client disconnects to stop
    recording. Requires the process auth token (a mic start is a side effect).
    """
    _require_token(request)
    if STATE.get("orch") is None:
        return JSONResponse({"error": "service not ready"}, status_code=503)
    max_seconds = _clamp(request.query_params.get("max_seconds"), MAXSEC_MIN, MAXSEC_MAX, 30.0)
    emit_interval = _clamp(request.query_params.get("emit_interval"), EMIT_MIN, EMIT_MAX, 0.8)

    async def event_gen():
        # Lease the *current* orchestrator for the whole stream so a concurrent
        # hot-swap cannot tear down the model mid-stream; a swap that starts
        # mid-flight is retried transparently. Inference runs in a worker
        # thread (run_in_executor) to keep the event loop responsive.
        async with _lease_current() as orch:
            loop = asyncio.get_event_loop()
            cap = MicCapture()
            # Helper startup performs a readiness handshake and can wait for a
            # microphone permission/device response. Keep it off the event loop;
            # if this generator is cancelled, stop() can terminate the published
            # process handle even while startup is still settling.
            try:
                await loop.run_in_executor(None, cap.start)
            except BaseException:
                await loop.run_in_executor(None, cap.stop)
                raise
            # `full_buf` always holds the COMPLETE capture (never dropped) so the
            # final ASR pass never loses audio. The bounded queue is only a
            # best-effort feed for low-latency partials; when it is full we drop
            # a partial, never the captured audio.
            full_buf: list[np.ndarray] = []
            q: _queue.Queue = _queue.Queue(maxsize=MAX_QUEUE)
            stop = {"t": False}
            disconnected = {"v": False}

            def producer() -> None:
                try:
                    for chunk in cap.iter_chunks(chunk_seconds=emit_interval):
                        if stop["t"]:
                            break
                        full_buf.append(chunk)
                        try:
                            q.put_nowait(chunk)
                        except _queue.Full:
                            # Slow partial consumer: drop only the realtime
                            # partial feed (never block the capture thread, or
                            # the audio pipe backs up and we lose tail audio),
                            # keep capturing the full audio in full_buf.
                            continue
                finally:
                    # No sentinel: the consumer exits once the producer thread
                    # ends and the queue drains, so a full queue can never strand
                    # the stream waiting for a dropped sentinel.
                    pass

            th = threading.Thread(target=producer, daemon=True)
            th.start()

            # Partial transcripts use a sliding window (last PARTIAL_WINDOW_SEC)
            # and are emitted at most every PARTIAL_MIN_INTERVAL seconds, so the
            # cost stays O(window) per emit instead of O(recording length).
            partial_chunks: list[np.ndarray] = []
            partial_samples = 0
            last_partial_t = 0.0
            partial_future: asyncio.Future | None = None
            deadline = time.perf_counter() + max_seconds
            yield _sse("start", {})
            try:
                # Run until capture is fully consumed (EOF / stop) and the
                # queue is empty — no sentinel to lose on a full queue.
                while th.is_alive() or not q.empty():
                    if await request.is_disconnected():
                        disconnected["v"] = True
                        break
                    if time.perf_counter() > deadline:
                        break

                    # Never await native partial inference inline: doing so
                    # prevents disconnect/deadline checks and can leave the mic
                    # recording after Stop. Poll at most one in-flight partial
                    # while capture and cancellation remain responsive.
                    if partial_future is not None and partial_future.done():
                        try:
                            tr = partial_future.result()
                        except Exception:  # noqa: BLE001
                            logger.exception("NCE partial transcription failed")
                            tr = None
                        partial_future = None
                        if tr is not None and tr.ok and tr.text.strip():
                            yield _sse("partial", {"text": tr.text, "compute": "ane"})

                    try:
                        chunk = await loop.run_in_executor(None, q.get, True, 0.1)
                    except _queue.Empty:
                        continue
                    partial_chunks.append(chunk)
                    partial_samples += len(chunk)
                    # Trim the window from the oldest end.
                    while partial_samples > PARTIAL_MAX_SAMPLES and partial_chunks:
                        partial_samples -= len(partial_chunks.pop(0))
                    now = time.perf_counter()
                    if (
                        partial_future is None
                        and now - last_partial_t >= PARTIAL_MIN_INTERVAL
                        and partial_samples > 16_000 * 0.4
                    ):
                        last_partial_t = now
                        window = (
                            np.concatenate(partial_chunks)
                            if partial_chunks
                            else np.zeros(0, dtype=np.float32)
                        )
                        # low-latency NCE partial (approximate; drops are fine)
                        if orch.funasr and len(window) > 16_000 * 0.4:
                            fn = functools.partial(
                                orch.funasr.transcribe,
                                window,
                                timeout_s=orch.funasr.timeout_s,
                            )
                            partial_future = loop.run_in_executor(None, fn)
            finally:
                stop["t"] = True
                await loop.run_in_executor(None, cap.stop)
                await loop.run_in_executor(None, th.join, 2.0)
                if partial_future is not None:
                    # The native call cannot be force-cancelled safely. Always
                    # consume its outcome (including when cancellation races a
                    # just-completed failure) so asyncio never reports an
                    # unretrieved Future exception. The per-engine lock makes
                    # shutdown wait for detached work safely.
                    def consume_late(fut: asyncio.Future) -> None:
                        if fut.cancelled():
                            return
                        try:
                            fut.exception()
                        except Exception:  # noqa: BLE001
                            pass

                    partial_future.add_done_callback(consume_late)

            # A very short recording may finish immediately after submitting
            # its first partial. Give an already-fast result a tiny flush window
            # after the mic is stopped, without ever delaying cancellation or
            # the recording deadline by an engine-sized timeout.
            if partial_future is not None and not disconnected["v"]:
                if not partial_future.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(partial_future), timeout=0.05)
                    except Exception:  # noqa: BLE001
                        pass
                if partial_future.done() and not partial_future.cancelled():
                    try:
                        tr = partial_future.result()
                    except Exception:  # noqa: BLE001
                        logger.exception("NCE partial transcription failed")
                        tr = None
                    if tr is not None and tr.ok and tr.text.strip():
                        yield _sse("partial", {"text": tr.text, "compute": "ane"})

            # The user hung up / hit stop: do NOT spend a full GPU/NPU
            # inference pass on audio no one will receive.
            if disconnected["v"]:
                yield _sse("end", {})
                return

            if not full_buf:
                yield _sse("error", {"error": "microphone produced no audio"})
                yield _sse("end", {})
                return

            # Final transcription uses the COMPLETE captured audio, independent
            # of any partials that were dropped under backpressure.
            full = np.concatenate(full_buf)
            try:
                result = await loop.run_in_executor(None, orch.transcribe, full)
                yield _sse("final", result.to_json())
            except Exception as exc:  # noqa: BLE001
                # Surface the failure as an explicit error event instead of an
                # abrupt connection drop (uses the orchestrator's exception path).
                logger.exception("streaming transcription failed")
                yield _sse("error", {"error": f"{type(exc).__name__}: {exc}"})
            finally:
                yield _sse("end", {})

    return StreamingResponse(event_gen(), media_type="text/event-stream")


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
