# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Audio ingestion for the macOS dual-engine ASR service.

Responsibilities:
  * Decode arbitrary input (wav/mp3/m4a/flac/opus...) to 16 kHz mono s16 PCM.
  * Resample + downmix to the 16 kHz / mono / s16 format both engines expect.
  * Lightweight energy-based VAD to drop silence before inference.
  * Real-time capture via a small AVAudioEngine helper (Swift) that streams
    16 kHz mono s16le PCM on stdout; we read it frame-by-frame.

The Apple Neural Engine (NCE) and the Metal GPU both want the same canonical
16k mono layout, so this module is the single normalization point.
"""

from __future__ import annotations

import asyncio
import logging
import os
import select
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterator

import numpy as np
import soundfile as sf

logger = logging.getLogger("voxkey.audio")

TARGET_RATE = 16_000
TARGET_CHANNELS = 1
CAPTURE_START_TIMEOUT_S = 5.0


# --------------------------------------------------------------------------- #
# Decoding / resampling
# --------------------------------------------------------------------------- #
def decode_to_16k_mono(data: bytes, *, tmp_dir: str | None = None) -> np.ndarray:
    """Decode raw audio bytes to a float32 16 kHz mono waveform.

    Uses ffmpeg (from the macOS Brewfile) for decoding + resampling so we don't
    need format-specific Python decoders. Returns samples in [-1, 1].
    """
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False, dir=tmp_dir) as src:
        src.write(data)
        src_path = src.name
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=tmp_dir) as dst:
        dst_path = dst.name
    try:
        _run_ffmpeg(
            [
                "-y",
                "-i",
                src_path,
                "-ar",
                str(TARGET_RATE),
                "-ac",
                str(TARGET_CHANNELS),
                "-sample_fmt",
                "s16",
                "-f",
                "wav",
                dst_path,
            ]
        )
        waveform, rate = sf.read(dst_path, dtype="float32", always_2d=False)
        if rate != TARGET_RATE:
            raise RuntimeError(f"ffmpeg did not resample to {TARGET_RATE} Hz (got {rate})")
        if waveform.ndim > 1:
            waveform = waveform.mean(axis=1)
        return waveform.astype(np.float32)
    finally:
        Path(src_path).unlink(missing_ok=True)
        Path(dst_path).unlink(missing_ok=True)


def waveform_to_wav_bytes(waveform: np.ndarray, rate: int = TARGET_RATE) -> bytes:
    """Serialize a float32 waveform to 16-bit PCM WAV bytes (for the LLM backend)."""
    import io

    buf = io.BytesIO()
    sf.write(buf, waveform.astype(np.float32), rate, format="WAV")
    return buf.getvalue()


def _run_ffmpeg(args: list[str]) -> None:
    """Run ffmpeg with the given arguments, raising on a non-zero exit code."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed ({proc.returncode}): {proc.stderr.decode().strip()[-300:]}"
        )


# --------------------------------------------------------------------------- #
# Voice activity detection (lightweight, energy + spectral flatness gate)
# --------------------------------------------------------------------------- #
@dataclass
class VADConfig:
    """Tuning for the energy-based voice activity detector."""

    frame_ms: int = 20  # analysis window
    threshold_db: float = -40.0  # RMS floor below which we treat as silence
    hangover_ms: int = 200  # keep speech on briefly after energy drops


def vad_segments(
    waveform: np.ndarray, rate: int = TARGET_RATE, cfg: VADConfig | None = None
) -> list[tuple[float, float]]:
    """Return [(start_s, end_s), ...] speech segments via energy gating.

    Cheap and dependency-free; good enough to drop long silences before sending
    audio to either engine. For stricter detection, swap in webrtcvad.
    """
    cfg = cfg or VADConfig()
    frame = int(rate * cfg.frame_ms / 1000)
    if len(waveform) < frame:
        return [(0.0, len(waveform) / rate)] if len(waveform) > 0 else []
    energies = []
    for i in range(0, len(waveform) - frame + 1, frame):
        chunk = waveform[i : i + frame]
        rms = np.sqrt(max(float(np.mean(chunk**2)), 1e-12))
        energies.append(10.0 * np.log10(rms))
    energies = np.array(energies, dtype=np.float32)
    active = energies > cfg.threshold_db
    # hangover: keep a little tail after the last speech frame
    hang = max(1, int(cfg.hangover_ms / cfg.frame_ms))
    for i in range(len(active) - 1, -1, -1):
        if active[i]:
            for j in range(i + 1, min(i + 1 + hang, len(active))):
                active[j] = True
            break
    segments: list[tuple[float, float]] = []
    i = 0
    n = len(active)
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i
        while j < n and active[j]:
            j += 1
        start = i * cfg.frame_ms / 1000.0
        end = min(j * cfg.frame_ms / 1000.0, len(waveform) / rate)
        if end - start > 0.1:
            segments.append((start, end))
        i = j
    return segments


def remove_silence(
    waveform: np.ndarray, rate: int = TARGET_RATE, cfg: VADConfig | None = None
) -> np.ndarray:
    segs = vad_segments(waveform, rate, cfg)
    if not segs:
        return waveform
    out = np.concatenate([waveform[int(s * rate) : int(e * rate)] for s, e in segs])
    return out.astype(np.float32)


# --------------------------------------------------------------------------- #
# Real-time capture via AVAudioEngine helper (Swift binary)
# --------------------------------------------------------------------------- #
_HELPER = Path(__file__).resolve().with_name("capture_helper")


class MicCapture:
    """Stream 16 kHz mono s16le frames from the default input device.

    Delegates to ``capture_helper`` (a tiny AVAudioEngine tap compiled from
    capture_helper.swift). The helper prints a 44-byte WAV header to stdout then
    streams raw PCM frames, so we can read incrementally without buffering the
    whole recording in memory.
    """

    def __init__(self, rate: int = TARGET_RATE, channels: int = TARGET_CHANNELS) -> None:
        self.rate = rate
        self.channels = channels
        self._proc: subprocess.Popen | None = None
        # Guards every access to `_proc` so `stop()` can run from the event
        # loop / StreamingResponse cleanup while a worker thread is blocked in
        # `read_frames`. Without this, `stop()` setting `_proc = None` races
        # with the reader touching `self._proc.stdout` -> AttributeError.
        self._lock = threading.Lock()
        self._eof = False

    def _ensure_helper(self) -> Path:
        if _HELPER.exists():
            return _HELPER
        raise RuntimeError(
            f"capture_helper not found at {_HELPER}. "
            "Compile it: swiftc -O capture_helper.swift -o capture_helper "
            "(requires Xcode command line tools)."
        )

    def is_running(self) -> bool:
        """Whether capture is currently active (helper still spawned)."""
        with self._lock:
            return self._proc is not None

    @staticmethod
    def _terminate_proc(proc: subprocess.Popen) -> None:
        """Terminate and reap ``proc``; never leave a killed helper as a zombie."""
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass

    @staticmethod
    def _read_exact_with_timeout(stdout, size: int, timeout_s: float) -> bytes:
        """Read up to ``size`` bytes from a pipe without blocking forever."""
        deadline = time.monotonic() + timeout_s
        fd = stdout.fileno()
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"capture_helper startup timed out after {timeout_s:.1f}s")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise TimeoutError(f"capture_helper startup timed out after {timeout_s:.1f}s")
            chunk = os.read(fd, size - len(data))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def start(self) -> None:
        # Publish the process handle immediately, then perform the blocking
        # readiness handshake outside the lock. A concurrent stop/cancellation
        # can therefore terminate a helper that stalls during startup.
        with self._lock:
            if self._proc is not None:
                return
            helper = self._ensure_helper()
            proc = subprocess.Popen(
                [str(helper), "--rate", str(self.rate), "--channels", str(self.channels)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            self._proc = proc
            self._eof = False

        stdout = proc.stdout
        if stdout is None:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            self._terminate_proc(proc)
            raise RuntimeError("capture_helper did not open stdout")

        try:
            # The Swift helper emits this header only AFTER AVAudioEngine starts,
            # making it a real readiness handshake rather than merely proof that
            # the child process launched.
            header = self._read_exact_with_timeout(stdout, 44, CAPTURE_START_TIMEOUT_S)
            if len(header) != 44 or proc.poll() is not None:
                raise RuntimeError("capture_helper exited before becoming ready")
            with self._lock:
                if self._proc is not proc:
                    raise RuntimeError("capture_helper was stopped during startup")
        except Exception as exc:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            self._terminate_proc(proc)
            detail = ""
            if proc.stderr is not None:
                try:
                    detail = proc.stderr.read().decode("utf-8", errors="replace").strip()[-300:]
                except (OSError, ValueError):
                    pass
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"capture_helper failed to start ({exc}){suffix}") from exc

    def stop(self) -> None:
        # Capture the process under the lock, release the lock BEFORE waiting,
        # so a worker thread blocked in read_frames can finish / hit EOF
        # without waiting on this lock. Then terminate + reap.
        with self._lock:
            proc = self._proc
            if proc is None:
                return
            self._proc = None
        self._terminate_proc(proc)

    def read_frames(self, n_frames: int) -> np.ndarray:
        """Read ``n_frames`` of mono float32 samples (blocking).

        Returns an empty array at end-of-stream (helper exited) so callers can
        detect EOF instead of busy-looping on a dead pipe. The process handle
        is captured once under the lock and the actual read happens outside it,
        so a concurrent `stop()` can never leave us dereferencing a `None`
        `_proc`.
        """
        with self._lock:
            proc = self._proc
            if proc is None:
                self._eof = True
                return np.zeros(0, dtype=np.float32)
            stdout = proc.stdout
        if stdout is None:
            self._eof = True
            return np.zeros(0, dtype=np.float32)
        n_bytes = n_frames * self.channels * 2
        raw = b""
        try:
            while len(raw) < n_bytes:
                chunk = stdout.read(n_bytes - len(raw))
                if chunk:
                    raw += chunk
                    continue
                # A blocking read only returns empty at EOF (the helper closed
                # stdout). Any buffered PCM has already been read, so we must
                # NOT break on `poll()` alone or we'd drop the tail still in
                # the pipe after the helper exits. If `stop()` terminated the
                # helper mid-read, the pipe closes and we also land here.
                break
        except (OSError, ValueError):
            # Pipe closed under us (capture stopped concurrently): end-of-stream.
            return np.zeros(0, dtype=np.float32)
        if not raw:
            self._eof = True
            return np.zeros(0, dtype=np.float32)
        # frombuffer needs an even byte count (int16 samples).
        raw = raw[: len(raw) // 2 * 2]
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        return samples

    def iter_chunks(self, chunk_seconds: float = 0.5) -> Iterator[np.ndarray]:
        """Yield fixed-length float32 chunks until stopped or EOF."""
        n = int(self.rate * chunk_seconds)
        if n <= 0:
            return
        while self.is_running():
            chunk = self.read_frames(n)
            if chunk.size == 0:
                # EOF (helper exited) or a degenerate 0-length request:
                # stop the capture and stop iterating instead of yielding
                # empty frames forever.
                self.stop()
                break
            yield chunk


async def aiter_chunks(cap: MicCapture, chunk_seconds: float = 0.5) -> AsyncIterator[np.ndarray]:
    loop = asyncio.get_event_loop()
    n = int(cap.rate * chunk_seconds)
    if n <= 0:
        return
    while cap.is_running():
        chunk = await loop.run_in_executor(None, cap.read_frames, n)
        if chunk.size == 0:
            cap.stop()
            break
        yield chunk
