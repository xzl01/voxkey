# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Concurrency / robustness tests for the macOS ASR service.

These exercise the hot-swap + in-flight safety, SSE cancel/error
handling, streaming-parameter bounds, capture EOF, and single-engine
failure paths that were previously unguarded. They use *fake* engines
(no real model load) so they run anywhere without torch/llama-cpp.

Run with:
    .venv/bin/python -m pytest test/test_service.py -q
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import threading
import time
import types
from dataclasses import dataclass
from unittest.mock import MagicMock

import numpy as np
import pytest

# --- stub the heavy engine modules so `service` imports without torch/llama-cpp ---
for _name, _cls in (("funasr_coreml", "FunASRCoreML"), ("qwen3_gpu", "Qwen3GPU")):
    _mod = types.ModuleType(_name)
    _mod.__dict__[_cls] = type(_cls, (), {})
    sys.modules[_name] = _mod

from common import EngineKind, Transcript  # noqa: E402
from orchestrator import DualEngineOrchestrator, FusionConfig  # noqa: E402

import service  # noqa: E402
from service import EngineHandle  # noqa: E402
import audio  # noqa: E402
from audio import MicCapture  # noqa: E402


# --------------------------------------------------------------------------- #
# Fake engines / capture
# --------------------------------------------------------------------------- #
@dataclass
class FakeEngine:
    """Stand-in for a real ASR engine (no model load)."""

    kind: EngineKind
    text: str = "hi"
    ok: bool = True
    fail: bool = False
    block: asyncio.Event | None = None  # when set, transcribe waits on it
    shutdown_called: bool = False

    def transcribe(self, waveform, *, language=None, timeout_s=None):
        if self.block is not None:
            self.block.wait(5)
        if self.fail:
            raise RuntimeError("boom")
        return Transcript(
            text=self.text, engine=self.kind, latency_s=0.0, ok=self.ok
        )

    @property
    def timeout_s(self):
        return 5.0

    def shutdown(self):
        self.shutdown_called = True


class FakeCapture:
    """Stand-in for MicCapture that replays a fixed list of chunks."""

    def __init__(self, chunks=None, rate=16_000, channels=1):
        self.chunks = list(chunks or [])
        self.rate = rate
        self.channels = channels
        self._proc = None

    def start(self):
        self._proc = object()

    def stop(self):
        self._proc = None

    def iter_chunks(self, chunk_seconds=0.5):
        for c in self.chunks:
            if self._proc is None:
                break
            yield c
        self._proc = None


def _make_orch(funasr_ok=True, funasr_fail=False, qwen3=False, block=None):
    funasr = FakeEngine(
        EngineKind.FUNASR_NCE, ok=funasr_ok, fail=funasr_fail, block=block
    )
    qw = FakeEngine(EngineKind.QWEN3_GPU) if qwen3 else None
    return DualEngineOrchestrator(funasr=funasr, qwen3=qw, fusion=FusionConfig())


# --------------------------------------------------------------------------- #
# EngineHandle: in-flight lease survives a concurrent hot-swap drain
# --------------------------------------------------------------------------- #
def test_handle_drains_only_after_inflight_releases():
    orch = _make_orch()
    h = EngineHandle(orch)

    async def coro():
        async with h.lease() as o:
            assert o is orch
            task = asyncio.create_task(h.drain(timeout=2))
            await asyncio.sleep(0.05)
            # still in-flight -> old engine must NOT be torn down yet
            assert not orch.funasr.shutdown_called
        # lease released -> drain can now shut it down
        await task
        assert orch.funasr.shutdown_called

    asyncio.run(coro())


def test_handle_drain_defers_shutdown_on_timeout():
    orch = _make_orch()
    h = EngineHandle(orch)

    async def coro():
        async with h.lease() as o:
            assert o is orch
            # A lease that never releases: drain must time out WITHOUT
            # force-shutting the active engine; it returns False instead.
            done = await asyncio.wait_for(h.drain(timeout=0.2), 1.0)
            assert done is False
            assert not orch.funasr.shutdown_called
            assert h._refs > 0  # still in-flight, engine preserved
        # Once the lease releases, a retry (the deferred reclaim path) drains
        # and shuts the engine down cleanly.
        await asyncio.wait_for(h.drain(timeout=2.0), 3.0)
        assert orch.funasr.shutdown_called

    asyncio.run(coro())


def test_lease_rejects_draining_handle_and_retries():
    orch_old = _make_orch()
    orch_new = _make_orch()
    old = EngineHandle(orch_old)
    new = EngineHandle(orch_new)

    async def coro():
        # Mark the old handle draining (a swap has completed its drain) before a
        # request tries to lease it. The lease must be refused and the caller
        # must re-acquire the *current* handle instead of blocking forever.
        async with old._cond:
            old._draining = True
        service.STATE["orch"] = new

        seen = {}

        async def grab():
            async with service._lease_current() as orch:
                seen["orch"] = orch

        await asyncio.wait_for(grab(), 2.0)
        assert seen["orch"] is orch_new
        # The draining old handle must never have been leased.
        assert old._refs == 0
        assert orch_new.funasr.shutdown_called is False

    try:
        asyncio.run(coro())
    finally:
        service.STATE.pop("orch", None)


# --------------------------------------------------------------------------- #
# Streaming parameter bounds
# --------------------------------------------------------------------------- #
def test_clamp_rejects_bad_values():
    # in-range garbage / non-finite -> default; finite out-of-range -> clamped
    assert service._clamp("0", 0.05, 5.0, 0.8) == 0.05  # clamped up to lo
    assert service._clamp("-3", 0.05, 5.0, 0.8) == 0.05
    assert service._clamp("nan", 0.05, 5.0, 0.8) == 0.8  # default
    assert service._clamp("inf", 0.05, 5.0, 0.8) == 0.8  # default
    assert service._clamp("abc", 0.05, 5.0, 0.8) == 0.8  # default
    assert service._clamp("0.8", 0.05, 5.0, 0.8) == 0.8
    assert service._clamp("10", 0.05, 5.0, 0.8) == 5.0  # clamped high
    assert service._clamp("0.1", 0.05, 5.0, 0.8) == 0.1  # kept


# --------------------------------------------------------------------------- #
# Capture EOF: iter_chunks / aiter_chunks must stop, not spin forever
# --------------------------------------------------------------------------- #
def test_iter_chunks_stops_on_eof():
    cap = MicCapture()
    cap._proc = MagicMock()
    # read_frames returns empty (EOF) -> iter_chunks must stop + stop capture
    cap.read_frames = lambda n: np.zeros(0, dtype=np.float32)
    out = list(cap.iter_chunks(chunk_seconds=0.5))
    assert out == []
    assert cap._proc is None  # capture stopped


def test_iter_chunks_ignores_zero_interval():
    cap = MicCapture()
    cap._proc = MagicMock()
    cap.read_frames = lambda n: np.zeros(0, dtype=np.float32)
    # n <= 0 must not busy-loop; yields nothing and returns
    assert list(cap.iter_chunks(chunk_seconds=0.0)) == []


def test_aiter_chunks_stops_on_eof():
    cap = MicCapture()
    cap._proc = MagicMock()
    cap.read_frames = lambda n: np.zeros(0, dtype=np.float32)

    async def coro():
        out = []
        async for c in audio.aiter_chunks(cap, chunk_seconds=0.5):
            out.append(c)
        return out

    assert asyncio.run(coro()) == []
    assert cap._proc is None


def test_read_frames_survives_concurrent_stop():
    entered = threading.Event()
    resume = threading.Event()

    class Stdout:
        calls = 0

        def read(self, _n):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert resume.wait(1)
            return b"\0\0"

    class Proc:
        stdout = Stdout()

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    cap = MicCapture()
    cap._proc = Proc()
    result = {}

    def reader():
        try:
            result["chunk"] = cap.read_frames(2)
        except Exception as exc:  # pragma: no cover - regression signal
            result["error"] = exc

    th = threading.Thread(target=reader)
    th.start()
    assert entered.wait(1)
    cap.stop()
    resume.set()
    th.join(1)
    assert not th.is_alive()
    assert "error" not in result
    assert len(result["chunk"]) == 2


def test_orchestrator_enforces_engine_timeout():
    class SlowEngine:
        kind = EngineKind.FUNASR_NCE
        timeout_s = 0.05

        def transcribe(self, waveform, *, language=None, timeout_s=None):
            time.sleep(0.3)
            return Transcript("late", self.kind, 0.3)

    orch = DualEngineOrchestrator(funasr=SlowEngine(), qwen3=None)
    started = time.perf_counter()
    with pytest.raises(RuntimeError, match="timeout after"):
        orch.transcribe(np.zeros(1, dtype=np.float32))
    assert time.perf_counter() - started < 0.2


# --------------------------------------------------------------------------- #
# HTTP-level behaviour via the in-process TestClient
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch, tmp_path):
    # Point the service at a temp config so POST /engines (which persists the
    # enablement map back to CONFIG_PATH) never mutates the real repo config.
    cfg = tmp_path / "_svc_cfg.json"
    cfg.write_text(
        json.dumps(
            {"engines": {"funasr_coreml": {"enabled": True}, "qwen3_gpu": {"enabled": True}}}
        )
    )
    monkeypatch.setattr(service, "CONFIG_PATH", cfg)
    # Keep the auth-token file out of the user's home during tests.
    monkeypatch.setenv("VOXKEY_ASR_TOKEN_FILE", str(tmp_path / "asr_token"))
    # fake engines + capture + decode (no real models / ffmpeg)
    monkeypatch.setattr(
        service,
        "load_engines",
        # honor the live ENGINE_CONFIG so POST /engines actually changes state
        lambda *a, **k: _make_orch(qwen3=service.ENGINE_CONFIG.get("qwen3_gpu", True)),
    )
    monkeypatch.setattr(service, "MicCapture", FakeCapture)
    monkeypatch.setattr(
        service, "decode_to_16k_mono", lambda data, **k: np.zeros(8000, dtype=np.float32)
    )

    from starlette.testclient import TestClient

    with TestClient(service.app) as c:
        # All requests in this client carry the live process token (the desktop
        # shell would obtain it from the token file and forward it).
        c.headers["X-VoxKey-Token"] = service.STATE.get("token", "")
        yield c


def _parse_sse(text):
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8")
    frames = []
    for block in text.split("\n\n"):
        ev = "message"
        data = []
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        # skip trailing/empty blocks (no data) so a phantom default
        # "message" event doesn't mask the real last frame.
        if data:
            frames.append((ev, "\n".join(data)))
    return frames


def test_transcribe_returns_fused_json(client, monkeypatch):
    res = client.post("/transcribe", content=b"audio-bytes")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["text"] == "hi"


def test_transcribe_single_engine_failure_is_500(client, monkeypatch):
    # FunASR (the only enabled engine) fails -> orchestrator escalates,
    # handler returns 500 instead of "success, empty text".
    # /transcribe uses STATE["orch"] (built at startup), so we swap it in
    # place rather than patching load_engines.
    service.STATE["orch"] = EngineHandle(_make_orch(funasr_fail=True, qwen3=False))
    res = client.post("/transcribe", content=b"audio-bytes")
    assert res.status_code == 500, res.text


def test_sse_happy_path_emits_start_partial_final_end(client, monkeypatch):
    # one chunk big enough to trigger a NCE partial (>6400 samples)
    monkeypatch.setattr(
        service, "MicCapture", lambda: FakeCapture(chunks=[np.zeros(8000, dtype=np.float32)])
    )
    with client.stream("GET", "/transcribe/stream") as r:
        text = r.read()
    frames = _parse_sse(text)
    events = [e for e, _ in frames]
    assert "start" in events
    assert "partial" in events
    assert "final" in events
    assert events[-1] == "end"


def test_sse_engine_error_is_surfaced_not_crashed(client, monkeypatch):
    # NCE partial fails -> skipped (no partial); final orchestrator raises
    # -> explicit "error" event + "end", not an abrupt disconnect.
    # Swap the live orchestrator in STATE (what /transcribe uses).
    service.STATE["orch"] = EngineHandle(_make_orch(funasr_fail=True, qwen3=False))
    monkeypatch.setattr(
        service, "MicCapture", lambda: FakeCapture(chunks=[np.zeros(8000, dtype=np.float32)])
    )
    with client.stream("GET", "/transcribe/stream") as r:
        text = r.read()
    frames = _parse_sse(text)
    events = [e for e, _ in frames]
    assert "error" in events
    assert events[-1] == "end"
    assert "final" not in events


def test_sse_bad_params_do_not_hang(client, monkeypatch):
    # emit_interval=0 / NaN max_seconds must be clamped, not busy-loop.
    monkeypatch.setattr(
        service, "MicCapture", lambda: FakeCapture(chunks=[np.zeros(8000, dtype=np.float32)])
    )
    with client.stream(
        "GET", "/transcribe/stream?emit_interval=0&max_seconds=nan"
    ) as r:
        text = r.read()
    frames = _parse_sse(text)
    events = [e for e, _ in frames]
    assert events[-1] == "end"


def test_sse_empty_capture_is_explicit_error(client, monkeypatch):
    monkeypatch.setattr(service, "MicCapture", lambda: FakeCapture(chunks=[]))
    with client.stream("GET", "/transcribe/stream") as r:
        frames = _parse_sse(r.read())
    events = [event for event, _data in frames]
    assert "error" in events
    assert "final" not in events
    assert events[-1] == "end"


def test_hot_swap_rebuild_keeps_service_up(client, monkeypatch):
    # swap to a config that keeps >=1 engine enabled; GET /engines later
    # must reflect the new state and not 500.
    res = client.post("/engines", json={"enabled": {"qwen3_gpu": False}})
    assert res.status_code == 200, res.text
    payload = res.json()
    qw = next(e for e in payload if e["id"] == "qwen3_gpu")
    assert qw["enabled"] is False
    # service still answers transcription requests
    assert client.post("/transcribe", content=b"x").status_code == 200


def test_hot_swap_rejects_all_disabled(client):
    res = client.post(
        "/engines", json={"enabled": {"funasr_coreml": False, "qwen3_gpu": False}}
    )
    assert res.status_code == 400


def test_endpoints_reject_bad_token(client):
    # Protected endpoints must reject a wrong/missing token; /health stays open.
    assert (
        client.post("/transcribe", content=b"x", headers={"X-VoxKey-Token": "nope"}).status_code
        == 401
    )
    assert (
        client.post(
            "/engines",
            json={"enabled": {"qwen3_gpu": False}},
            headers={"X-VoxKey-Token": "nope"},
        ).status_code
        == 401
    )
    assert client.get("/health").status_code == 200


def test_cors_is_not_wildcard(client):
    # A cross-origin request must not get a wildcard ACAO; the service only
    # allows the desktop shell's own origins.
    res = client.get("/health", headers={"Origin": "http://evil.example.com"})
    assert res.headers.get("access-control-allow-origin") != "*"


def test_hot_swap_reuses_unchanged_engine(client, monkeypatch):
    # Disabling qwen3 while funasr stays on must reuse the already-loaded
    # funasr object (no re-warm), not rebuild everything.
    def fake_load(enabled_override=None, reuse_from=None):
        funasr = (
            reuse_from.funasr
            if (reuse_from and reuse_from.funasr is not None)
            else FakeEngine(EngineKind.FUNASR_NCE)
        )
        qwen3 = (
            FakeEngine(EngineKind.QWEN3_GPU)
            if (enabled_override or {}).get("qwen3_gpu", True)
            else None
        )
        return DualEngineOrchestrator(funasr=funasr, qwen3=qwen3, fusion=FusionConfig())

    monkeypatch.setattr(service, "load_engines", fake_load)
    first = service.STATE["orch"].orch.funasr
    res = client.post("/engines", json={"enabled": {"qwen3_gpu": False}})
    assert res.status_code == 200
    # The live funasr must be the SAME object as before the swap (reused).
    assert service.STATE["orch"].orch.funasr is first


def test_transcribe_rejects_oversized_body(client):
    # Content-Length above the cap is rejected before any decoding/read.
    res = client.post(
        "/transcribe",
        content=b"small",
        headers={"Content-Length": str(service.MAX_BODY_BYTES + 1)},
    )
    assert res.status_code == 413


def test_read_frames_yields_tail_then_eof():
    cap = MicCapture()
    cap._proc = MagicMock()
    calls = {"n": 0}

    def rf(n):
        calls["n"] += 1
        # First read returns a partial frame; the next returns empty (EOF).
        return np.zeros(8000, dtype=np.float32) if calls["n"] == 1 else np.zeros(0, dtype=np.float32)

    cap.read_frames = rf
    out = list(cap.iter_chunks(chunk_seconds=0.5))
    # The partial tail must be yielded, not dropped, before EOF stops capture.
    assert len(out) == 1
    assert cap._proc is None


def test_engines_body_rejects_non_object_and_bad_bool(client):
    # A non-object / null body must be rejected (422), not 500; a non-bool
    # scalar must be rejected rather than silently coerced by bool().
    assert client.post("/engines", content=b"null",
                       headers={"Content-Type": "application/json"}).status_code == 422
    assert client.post("/engines", json=[1, 2, 3]).status_code == 422
    assert client.post("/engines", json={"enabled": "false"}).status_code == 422
    assert (
        client.post("/engines", json={"enabled": {"funasr_coreml": "false"}}).status_code
        == 422
    )
    # A genuine boolean is still accepted and persists.
    res = client.post("/engines", json={"enabled": {"qwen3_gpu": False}})
    assert res.status_code == 200, res.text
    qw = next(e for e in res.json() if e["id"] == "qwen3_gpu")
    assert qw["enabled"] is False


def test_engines_rejects_unknown_id_and_skips_empty_swap(client, monkeypatch):
    assert (
        client.post("/engines", json={"enabled": {"typo_engine": False}}).status_code
        == 422
    )
    called = {"n": 0}

    def should_not_load(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("empty update must not rebuild engines")

    monkeypatch.setattr(service, "load_engines", should_not_load)
    res = client.post("/engines", json={"enabled": {}})
    assert res.status_code == 200
    assert called["n"] == 0


def test_publish_token_atomically_replaces_stale_file(tmp_path, monkeypatch):
    primary = tmp_path / "primary" / "asr_token"
    primary.parent.mkdir(parents=True)
    primary.write_text("OLD-TOKEN")
    primary.chmod(0o444)
    monkeypatch.setenv("VOXKEY_ASR_TOKEN_FILE", str(primary))
    written = service._publish_token("new-token")
    assert written == primary
    assert written.read_text() == "new-token"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600


def test_publish_token_failure_has_no_ambiguous_fallback(tmp_path, monkeypatch):
    primary = tmp_path / "asr_token"
    primary.write_text("OLD-TOKEN")
    monkeypatch.setenv("VOXKEY_ASR_TOKEN_FILE", str(primary))
    monkeypatch.setattr(
        service.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("read-only")),
    )
    with pytest.raises(PermissionError, match="read-only"):
        service._publish_token("new-token")
    assert primary.read_text() == "OLD-TOKEN"
    assert not (tmp_path / ".asr_token").exists()


def test_token_override_must_be_absolute(monkeypatch):
    monkeypatch.setenv("VOXKEY_ASR_TOKEN_FILE", "relative/asr_token")
    with pytest.raises(ValueError, match="absolute path"):
        service._token_file_path()


def test_token_is_header_only_and_missing_state_fails_closed(client):
    live = service.STATE["token"]
    res = client.post(
        f"/transcribe?token={live}",
        content=b"x",
        headers={"X-VoxKey-Token": ""},
    )
    assert res.status_code == 401

    service.STATE.pop("token", None)
    try:
        assert client.post("/transcribe", content=b"x").status_code == 503
    finally:
        service.STATE["token"] = live
