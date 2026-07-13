# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Windows push-to-talk voice input daemon for VoxKey.

Flow:
  * A global hotkey (the `keyboard` library) starts/stops recording.
  * While held, WASAPI (via `sounddevice`) captures 16 kHz mono s16 PCM.
  * On release we hand the WAV to the ASR backend (HTTP ``/transcribe`` or the
    local Qwen3-ASR engine) and commit the result via clipboard + Ctrl+V, which
    is IME-safe for Chinese text regardless of the active input method.
  * Optional Windows toast notifications via `plyer`.

This is the Windows counterpart of back-end/platforms/macos/macos_daemon.py and
back-end/voice-daemon/voice_input_daemon.py. It deliberately avoids Linux-only
modules (evdev, fcntl) so it imports cleanly on Windows.

Run:
  python windows_daemon.py --config back-end/platforms/windows/config.json
  python windows_daemon.py --self-test
  python windows_daemon.py --transcribe-file clip.wav
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

# Optional dependencies are guarded so the daemon can at least self-test and
# report what is missing instead of crashing on import.
try:
    import keyboard
except Exception:  # pragma: no cover - platform dependent
    keyboard = None
try:
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None
try:
    import soundfile as sf  # noqa: F401  (kept for optional file capture paths)
except Exception:  # pragma: no cover
    sf = None
try:
    import pyperclip
except Exception:  # pragma: no cover
    pyperclip = None
try:
    import requests
except Exception:  # pragma: no cover
    requests = None
try:
    from plyer import notification as plyer_notify
except Exception:  # pragma: no cover
    plyer_notify = None

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
logger = logging.getLogger("voxkey.daemon")

WIN_DIR = Path(__file__).resolve().parent

# UI <-> daemon config bridge (camelCase Tauri settings -> snake_case config).
# Mirrors the Linux daemon's apply_ui_settings.
UI_MAP = {
    "asrBackend": "asr_backend",
    "asrServiceUrl": "asr_service_url",
    "asrFallbackLocal": "asr_fallback_local",
    "asrHttpTimeout": "asr_http_timeout",
    "selectedRuntimeId": "selected_runtime_id",
}


def expand(p: str | None) -> str | None:
    """Expand ``~`` and ``%VAR%`` Windows environment variables in a path."""
    if p is None:
        return None
    return os.path.expandvars(os.path.expanduser(p))


def load_config(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def default_ui_settings_path() -> Path:
    return Path(os.environ.get("APPDATA", "")).joinpath("dev.xzl01.voxkey", "settings.json")


def apply_ui_settings(cfg: dict) -> dict:
    """Overlay the desktop UI's Tauri ``settings.json`` onto ``cfg``.

    The GUI is the single source of truth on the same machine, so the daemon
    reads it after ``config.json`` and overrides the matching fields.
    """
    override = cfg.get("ui_settings_path") or str(default_ui_settings_path())
    pp = Path(expand(override)) if override else None
    if not pp or not pp.exists():
        return cfg
    try:
        ui = json.loads(pp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read UI settings %s: %s", pp, exc)
        return cfg
    for camel, snake in UI_MAP.items():
        if camel in ui and ui[camel] is not None:
            cfg[snake] = ui[camel]
    logger.info("Applied UI settings override from %s", pp)
    return cfg


# --------------------------------------------------------------------------- #
# Text commit (clipboard + Ctrl+V is IME-safe for CJK on Windows)
# --------------------------------------------------------------------------- #
def notify(text: str, title: str = "简听输入") -> None:
    if plyer_notify is None:
        return
    try:
        plyer_notify.notify(title=title, message=text[:200])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Notification failed: %s", exc)


def commit_text(text: str, *, notify_flag: bool = True) -> None:
    if not text:
        logger.info("Empty transcription; nothing to commit")
        return
    if pyperclip is not None:
        try:
            pyperclip.copy(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Clipboard copy failed: %s", exc)
    else:
        logger.warning("pyperclip unavailable; cannot copy to clipboard")
    if keyboard is not None:
        try:
            # Paste into the currently focused window. IME-safe for Chinese.
            keyboard.send("ctrl+v")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Paste failed: %s", exc)
    else:
        logger.warning("keyboard library unavailable; text not typed")
    logger.info("Committed: %s", text)
    if notify_flag:
        notify(text)


# --------------------------------------------------------------------------- #
# Audio capture (WASAPI via sounddevice)
# --------------------------------------------------------------------------- #
class Recorder:
    """Accumulate 16 kHz mono int16 PCM from the default WASAPI input."""

    def __init__(self, cfg: dict) -> None:
        cap = cfg.get("capture", {})
        self.rate = int(cap.get("rate", 16000))
        self.channels = int(cap.get("channels", 1))
        self.device = cap.get("device") or None
        self.stream = None
        self.frames: list[np.ndarray] = []
        self.lock = threading.Lock()

    def start(self) -> None:
        if self.stream is not None:
            return
        if sd is None:
            raise RuntimeError("sounddevice not available; cannot record")
        self.frames = []

        def cb(indata, _frames, _time_info, status):
            if status:
                logger.debug("sounddevice status: %s", status)
            with self.lock:
                self.frames.append(indata.copy())

        self.stream = sd.InputStream(
            samplerate=self.rate,
            channels=self.channels,
            device=self.device,
            dtype="int16",
            callback=cb,
        )
        self.stream.start()
        logger.info("Recording started (WASAPI, %d Hz mono)", self.rate)

    def stop(self) -> np.ndarray:
        if self.stream is None:
            return np.zeros((0, self.channels), dtype=np.int16)
        self.stream.stop()
        self.stream.close()
        self.stream = None
        with self.lock:
            data = (
                np.concatenate(self.frames, axis=0)
                if self.frames
                else np.zeros((0, self.channels), dtype=np.int16)
            )
        logger.info("Recording stopped (%d samples)", data.shape[0])
        return data

    def save_wav(self, path: Path) -> Path:
        data = self.stop()
        with wave.open(str(path), "wb") as w:
            w.setnchannels(self.channels)
            w.setsampwidth(2)  # 16-bit
            w.setframerate(self.rate)
            w.writeframes(data.tobytes())
        return path


# --------------------------------------------------------------------------- #
# ASR backend (HTTP /transcribe or local Qwen3-ASR)
# --------------------------------------------------------------------------- #
class ASR:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.backend = (cfg.get("asr_backend") or "http").lower().strip()
        self.engine = None
        self.orchestrator = None
        if self.backend == "local":
            self._load_local()

    def _load_local(self) -> None:
        """Load the local dual-engine (Qwen3 Vulkan + FunASR DirectML).

        ``selected_runtime_id`` steers the Qwen3 ONNX encoder provider:
          * ``gpu-directml`` -> ``onnx_provider="Dml"`` (encoder on the AMD GPU)
          * anything else     -> ``onnx_provider="CPU"`` (decoder still on Vulkan)
        The HTTP fallback path (``transcribe``) is unchanged.
        """
        try:
            # Reuse the exact same engine loader as the FastAPI service so the
            # daemon and the local API never diverge.
            sys.path.insert(0, str(WIN_DIR))
            from service import load_engines  # type: ignore

            # Mirror runtime selection into the engines config the loader reads.
            runtime = (self.cfg.get("selected_runtime_id") or "").lower()
            onnx_provider = "Dml" if runtime == "gpu-directml" else "CPU"
            raw_cfg = (WIN_DIR / "config.json")
            if raw_cfg.is_file():
                raw = json.loads(raw_cfg.read_text(encoding="utf-8"))
            else:
                raw = {}
            engines = raw.setdefault("engines", {})
            qc = engines.setdefault("qwen3_gpu", {})
            qc["onnx_provider"] = onnx_provider
            qc.setdefault("enabled", True)
            engines.setdefault("funasr_directml", {}).setdefault("enabled", True)
            raw_cfg.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

            self.orchestrator = load_engines()
            self.engine = None  # dual-engine path uses self.orchestrator
            logger.info("Local dual-engine (Qwen3 Vulkan + FunASR DirectML) ready")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Local dual-engine failed to load: %s", exc)
            self.orchestrator = None
            self.engine = None

    def transcribe(self, wav_path: Path) -> str:
        if self.backend == "http":
            try:
                return self._transcribe_http(wav_path)
            except Exception as exc:  # noqa: BLE001
                if self.cfg.get("asr_fallback_local"):
                    logger.warning("HTTP ASR failed (%s); falling back to local", exc)
                    return self._transcribe_local(wav_path)
                raise
        return self._transcribe_local(wav_path)

    def _transcribe_http(self, wav_path: Path) -> str:
        if requests is None:
            raise RuntimeError("requests not available for HTTP ASR backend")
        url = (self.cfg.get("asr_service_url") or "http://127.0.0.1:17863").rstrip("/") + "/transcribe"
        with open(wav_path, "rb") as f:
            resp = requests.post(
                url,
                files={"file": (Path(wav_path).name, f, "audio/wav")},
                timeout=self.cfg.get("asr_http_timeout", 30),
            )
        resp.raise_for_status()
        payload = resp.json()
        text = payload.get("text") or payload.get("transcription") or ""
        return text.strip()

    def _transcribe_local(self, wav_path: Path) -> str:
        if getattr(self, "orchestrator", None) is None:
            # Lazily load the dual-engine so the HTTP->local fallback (which only
            # fires when backend=="http") can still recover offline.
            try:
                self._load_local()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("local dual-engine not loaded") from exc
        import soundfile as sf

        waveform, rate = sf.read(str(wav_path), dtype="float32", always_2d=False)
        if waveform.ndim > 1:
            waveform = waveform.mean(axis=1)
        if rate != 16_000:
            try:
                from scipy.signal import resample

                waveform = resample(waveform, int(round(len(waveform) * 16_000 / rate)))
            except Exception:
                pass
        result = self.orchestrator.transcribe(
            waveform.astype(np.float32), language=self.cfg.get("language")
        )
        return (result.final_text or "").strip()


# --------------------------------------------------------------------------- #
# Daemon: hotkey state machine + commit
# --------------------------------------------------------------------------- #
class Daemon:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.rec = Recorder(cfg)
        self.asr = ASR(cfg)
        self.recording = False

    def _audio_path(self) -> Path:
        d = Path(expand(self.cfg.get("recordings_dir")) or (WIN_DIR / "recordings"))
        d.mkdir(parents=True, exist_ok=True)
        return d / f"voice-{time.strftime('%Y%m%d-%H%M%S')}.wav"

    def on_press(self) -> None:
        if self.recording:
            return
        self.rec.start()
        self.recording = True

    def on_release(self) -> None:
        if not self.recording:
            return
        self.recording = False
        data = self.rec.stop()
        min_samples = int(self.cfg.get("min_record_seconds", 0.25) * self.rec.rate)
        if data.shape[0] < min_samples:
            logger.info("Recording too short (%d < %d); ignored", data.shape[0], min_samples)
            return
        path = self._audio_path()
        with wave.open(str(path), "wb") as w:
            w.setnchannels(self.rec.channels)
            w.setsampwidth(2)
            w.setframerate(self.rec.rate)
            w.writeframes(data.tobytes())
        logger.info("Transcribing %s ...", path)
        try:
            text = self.asr.transcribe(path)
        except Exception as exc:  # noqa: BLE001
            logger.error("ASR failed: %s", exc)
            return
        if self.cfg.get("strip_trailing_punctuation"):
            text = text.rstrip("，。！？、,.;:!? ")
        if self.cfg.get("type_text", True):
            commit_text(text, notify_flag=self.cfg.get("notify", True))
        else:
            logger.info("Result: %s", text)

    def run(self) -> int:
        trig = self.cfg.get("trigger", {})
        if not trig.get("enabled", False):
            logger.warning("trigger.enabled is false; not listening. Set it to true in config.json")
            return 0
        if keyboard is None:
            logger.error("keyboard library unavailable; cannot listen for hotkey")
            return 2

        key = trig.get("key") or "right_shift"
        mode = (trig.get("mode") or "hold").lower().strip()
        logger.info("Listening on key %r (mode=%s). Press Ctrl+C to exit.", key, mode)

        if mode == "toggle":
            state = {"on": False}

            def toggle(_event):
                if not state["on"]:
                    state["on"] = True
                    self.on_press()
                else:
                    state["on"] = False
                    self.on_release()

            keyboard.on_press_key(key, toggle)
        else:  # hold
            keyboard.on_press_key(key, lambda _e: self.on_press())
            keyboard.on_release_key(key, lambda _e: self.on_release())

        try:
            keyboard.wait()
        except KeyboardInterrupt:
            pass
        finally:
            if self.rec.stream is not None:
                self.rec.stream.close()
        return 0

    def self_test(self) -> int:
        logger.info("== Self-test ==")
        logger.info("keyboard   : %s", "ok" if keyboard else "MISSING")
        logger.info("sounddevice: %s", "ok" if sd else "MISSING")
        logger.info("soundfile  : %s", "ok" if sf else "MISSING")
        logger.info("pyperclip  : %s", "ok" if pyperclip else "MISSING")
        logger.info("requests   : %s", "ok" if requests else "MISSING")
        logger.info("plyer      : %s", "ok" if plyer_notify else "MISSING")
        if sd is not None:
            try:
                devs = sd.query_devices()
                logger.info("Audio devices: %d (default input = %s)", len(devs), sd.default.device)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Audio device query failed: %s", exc)
        if self.cfg.get("trigger", {}).get("enabled") and keyboard is None:
            return 2
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="VoxKey Windows voice input daemon")
    ap.add_argument("--config", default=str(WIN_DIR / "config.json"))
    ap.add_argument("--self-test", action="store_true", help="run diagnostics and exit")
    ap.add_argument("--transcribe-file", metavar="WAV", help="transcribe a WAV file and print/commit")
    args = ap.parse_args()

    cfg = load_config(Path(expand(args.config)))
    cfg = apply_ui_settings(cfg)
    asr = ASR(cfg)

    if args.transcribe_file:
        path = Path(expand(args.transcribe_file))
        text = asr.transcribe(path)
        if cfg.get("strip_trailing_punctuation"):
            text = text.rstrip("，。！？、,.;:!? ")
        print(text)
        if cfg.get("type_text", True):
            commit_text(text, notify_flag=cfg.get("notify", True))
        return 0

    daemon = Daemon(cfg)
    if args.self_test:
        return daemon.self_test()
    return daemon.run()


if __name__ == "__main__":
    raise SystemExit(main())
