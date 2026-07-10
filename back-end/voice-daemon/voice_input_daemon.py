#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Linux push-to-talk voice input daemon for Qwen3-ASR-GGUF.

Behavior inspired by CapsWriter-Offline:
  hold mode:   press configured key -> start recording; release -> stop/transcribe/type
  toggle mode: first press -> start recording; next press -> stop/transcribe/type

Text is committed through a local fcitx5 addon first when enabled; wtype remains
the fallback path.

This implementation is Wayland/niri-friendly and reads the configured hardware
key directly from /dev/input/event*. It intentionally avoids global GUI hotkey
libraries because some laptop special keys are exposed by auxiliary input
devices, not the normal keyboard.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import queue
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


EV_KEY = 0x01
KEY_RELEASE = 0
KEY_PRESS = 1
KEY_REPEAT = 2
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)


@dataclasses.dataclass(frozen=True)
class TriggerConfig:
    enabled: bool = False
    backend: str = "evdev"
    input_name: Optional[str] = None
    input_device: Optional[str] = None
    code: Optional[int] = None
    name: str = "voice input key"
    mode: str = "hold"


@dataclasses.dataclass(frozen=True)
class Config:
    trigger: TriggerConfig
    recordings_dir: str
    asr_project_dir: str
    model_dir: str
    python_venv: str
    language: Optional[str]
    min_record_seconds: float
    pw_record: dict
    type_command: str
    copy_to_clipboard: bool
    type_text: bool
    notify: bool
    notify_timeout_ms: int
    strip_trailing_punctuation: bool
    fcitx_commit: bool = True
    fcitx_socket: Optional[str] = None
    fcitx_commit_timeout_ms: int = 500
    asr_backend: str = "local"
    asr_service_url: str = "http://127.0.0.1:17863"
    asr_fallback_local: bool = True
    asr_http_timeout: int = 30
    # UI bridge: the desktop UI writes its selections into Tauri's settings.json.
    # `selected_runtime_id` is consumed by QwenAsr to pick the ONNX provider / GPU
    # path; `ui_settings_path` optionally points at that file to override the
    # platform-default location.
    selected_runtime_id: Optional[str] = None
    ui_settings_path: Optional[str] = None


def expand_path(value: Any, base_dir: Path) -> Any:
    if value is None:
        return None
    expanded = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if not expanded.is_absolute():
        expanded = base_dir / expanded
    return str(expanded)


def load_trigger_config(raw: dict[str, Any], base_dir: Path) -> TriggerConfig:
    if isinstance(raw.get("trigger"), dict):
        trigger_raw = dict(raw["trigger"])
    else:
        legacy_has_trigger = "trigger_code" in raw or "input_device" in raw
        trigger_raw = {
            "enabled": legacy_has_trigger,
            "backend": "evdev",
            "input_name": raw.get("input_name"),
            "input_device": raw.get("input_device"),
            "code": raw.get("trigger_code"),
            "name": raw.get("trigger_name", "voice input key"),
            "mode": raw.get("trigger_mode", "hold"),
        }

    if trigger_raw.get("input_device") is not None:
        trigger_raw["input_device"] = expand_path(trigger_raw["input_device"], base_dir)

    allowed = {field.name for field in dataclasses.fields(TriggerConfig)}
    return TriggerConfig(**{key: value for key, value in trigger_raw.items() if key in allowed})


def default_ui_settings_path() -> Optional[Path]:
    """Platform-default location of the desktop UI's Tauri settings.json."""
    identifier = "dev.xzl01.voxkey"
    if sys.platform.startswith("linux"):
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return Path(base) / identifier / "settings.json"
    if sys.platform == "darwin":
        return Path(os.path.expanduser("~/Library/Application Support")) / identifier / "settings.json"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
        return Path(base) / identifier / "settings.json"
    return None


def apply_ui_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Overlay the desktop UI's Tauri settings onto the daemon config.

    The desktop UI is the user-facing place to pick the ASR backend and compute
    runtime, but it persists into Tauri's ``settings.json`` while the daemon reads
    ``config.json``. When both run on the same machine, the UI selection must win
    so the GUI is the single source of truth.
    """
    explicit = raw.get("ui_settings_path")
    path = Path(expand_path(explicit, Path.cwd())) if explicit else default_ui_settings_path()
    if path is None or not path.is_file():
        return raw
    try:
        ui = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"UI settings at {path} unreadable ({exc}); using config.json only")
        return raw

    # Tauri serializes DesktopSettings with snake_case keys (serde default), so
    # the UI's settings.json matches the daemon's config.json casing. Overlay
    # using the same snake_case keys the UI actually writes.
    mapping = {
        "asr_backend": "asr_backend",
        "asr_service_url": "asr_service_url",
        "asr_fallback_local": "asr_fallback_local",
        "asr_http_timeout": "asr_http_timeout",
        "selected_runtime_id": "selected_runtime_id",
    }
    overridden = []
    for ui_key, cfg_key in mapping.items():
        if ui.get(ui_key) is not None:
            raw[cfg_key] = ui[ui_key]
            overridden.append(cfg_key)
    if overridden:
        log(f"Applied UI settings from {path}: {', '.join(overridden)}")
    return raw


def load_config(path: Path) -> Config:
    path = path.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    base_dir = path.parent
    for key in {
        "recordings_dir",
        "asr_project_dir",
        "model_dir",
        "python_venv",
        "fcitx_socket",
    }:
        if raw.get(key) is None:
            continue
        raw[key] = expand_path(raw[key], base_dir)

    raw["trigger"] = load_trigger_config(raw, base_dir)
    raw = apply_ui_settings(raw)
    allowed = {field.name for field in dataclasses.fields(Config)}
    cfg = Config(**{key: value for key, value in raw.items() if key in allowed})
    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    if cfg.min_record_seconds < 0:
        raise ValueError("min_record_seconds must be >= 0")
    pr = cfg.pw_record
    if not isinstance(pr, dict):
        raise ValueError("pw_record must be an object")
    rate = pr.get("rate", 16000)
    channels = pr.get("channels", 1)
    if not isinstance(rate, int) or rate <= 0:
        raise ValueError("pw_record.rate must be a positive integer")
    if not isinstance(channels, int) or channels <= 0:
        raise ValueError("pw_record.channels must be a positive integer")
    if cfg.notify_timeout_ms < 0:
        raise ValueError("notify_timeout_ms must be >= 0")
    if cfg.fcitx_commit_timeout_ms <= 0:
        raise ValueError("fcitx_commit_timeout_ms must be > 0")
    mode = (cfg.trigger.mode or "").lower().strip()
    if mode and mode not in {"hold", "toggle"}:
        raise ValueError(f"trigger.mode must be 'hold' or 'toggle', got {cfg.trigger.mode!r}")
    backend = (cfg.asr_backend or "local").lower().strip()
    if backend not in {"local", "http"}:
        raise ValueError(f"asr_backend must be 'local' or 'http', got {cfg.asr_backend!r}")
    if backend == "http":
        url = (cfg.asr_service_url or "").lower()
        if not url.startswith(("http://", "https://")):
            raise ValueError("asr_service_url must be an http(s) URL when asr_backend is 'http'")
        if cfg.asr_http_timeout <= 0:
            raise ValueError("asr_http_timeout must be > 0")


_logger = logging.getLogger("voxkey")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def log(message: str) -> None:
    _logger.info(message)


def run_checked(args: list[str], *, input_text: str | None = None, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def strip_punctuation(text: str) -> str:
    return text.rstrip("。．.，,、！？!?；;：:\n\r\t ")


def runtime_dir() -> Path:
    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime_dir and os.access(xdg_runtime_dir, os.W_OK | os.X_OK):
        return Path(xdg_runtime_dir)
    run_user_dir = Path("/run/user") / str(os.getuid())
    if run_user_dir.exists() and os.access(run_user_dir, os.W_OK | os.X_OK):
        return run_user_dir
    return Path(tempfile.gettempdir())


def fcitx_socket_path(cfg: Config) -> Path:
    if cfg.fcitx_socket:
        return Path(cfg.fcitx_socket).expanduser()
    return runtime_dir() / "voxkey-fcitx.sock"


def send_fcitx_request(cfg: Config, payload: bytes, *, reply_size: int = 256) -> tuple[bool, str]:
    server_path = fcitx_socket_path(cfg)
    client_path = runtime_dir() / f"voxkey-client-{os.getpid()}-{time.monotonic_ns()}.sock"
    timeout = max(cfg.fcitx_commit_timeout_ms, 1) / 1000.0

    try:
        client_path.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.bind(str(client_path))
            sock.sendto(payload, str(server_path))
            data, _addr = sock.recvfrom(reply_size)
    except (OSError, socket.timeout) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            client_path.unlink(missing_ok=True)
        except OSError:
            pass

    return True, data.decode("utf-8", errors="replace").strip()


def ping_fcitx(cfg: Config) -> tuple[bool, str]:
    ok, reply = send_fcitx_request(cfg, b"")
    if ok and reply == "PONG":
        return True, "PONG"
    return False, reply


def commit_text_with_fcitx(cfg: Config, text: str) -> tuple[bool, str]:
    if not cfg.fcitx_commit:
        return False, "fcitx commit disabled"

    ok, reply = send_fcitx_request(cfg, b"COMMIT\n" + text.encode("utf-8"))
    if not ok:
        return False, reply
    if reply == "OK":
        return True, "OK"
    return False, reply or "empty fcitx reply"


def key_state_name(state: int) -> str:
    if state == KEY_RELEASE:
        return "UP"
    if state == KEY_PRESS:
        return "DOWN"
    if state == KEY_REPEAT:
        return "REPEAT"
    return f"VALUE_{state}"


def iter_input_devices() -> list[dict[str, str]]:
    try:
        content = Path("/proc/bus/input/devices").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    devices: list[dict[str, str]] = []
    for block in content.split("\n\n"):
        name_match = re.search(r'N:\s+Name="([^"]+)"', block)
        handlers_match = re.search(r'H:\s+Handlers=(.*)', block)
        event_match = re.search(r'H:\s+Handlers=.*\bevent(\d+)\b', block)
        if not name_match or not event_match:
            continue
        devices.append({
            "path": f"/dev/input/event{event_match.group(1)}",
            "name": name_match.group(1),
            "handlers": handlers_match.group(1).strip() if handlers_match else "",
        })
    return devices


def print_input_devices() -> int:
    devices = iter_input_devices()
    if not devices:
        print("No /dev/input/event* devices found via /proc/bus/input/devices")
        return 1

    for device in devices:
        readable = "readable" if os.access(device["path"], os.R_OK) else "no-read-access"
        print(f'{device["path"]}\t{readable}\t{device["name"]}')
    return 0


def find_input_device_by_name(name: str) -> Optional[str]:
    for device in iter_input_devices():
        device_name = device["name"]
        if name != device_name and name not in device_name:
            continue
        return device["path"]
    return None


def resolve_input_device(trigger: TriggerConfig) -> Optional[str]:
    if trigger.input_name:
        device = find_input_device_by_name(trigger.input_name)
        if device:
            return device
        log(f"Input device named {trigger.input_name!r} not found; using fallback {trigger.input_device}")
    return trigger.input_device


def detect_key(device_path: Optional[str], timeout: float) -> int:
    candidate_devices = [
        device for device in iter_input_devices()
        if device_path is None or device["path"] == device_path
    ]
    if device_path and not candidate_devices:
        candidate_devices = [{"path": device_path, "name": device_path, "handlers": ""}]

    poller = select.poll()
    fds: dict[int, dict[str, str]] = {}
    for device in candidate_devices:
        try:
            fd = os.open(device["path"], os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        poller.register(fd, select.POLLIN)
        fds[fd] = device

    if not fds:
        print("No readable input devices. Check device permissions or pass --detect-key /dev/input/eventX.", file=sys.stderr)
        return 1

    print("Press the key you want to use as the voice input trigger...", flush=True)
    deadline = time.monotonic() + timeout
    pressed: Optional[dict[str, Any]] = None
    saw_repeat = False

    try:
        while time.monotonic() < deadline:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            for fd, _event in poller.poll(remaining_ms):
                try:
                    data = os.read(fd, EVENT_SIZE * 16)
                except BlockingIOError:
                    continue
                device = fds[fd]
                for offset in range(0, len(data), EVENT_SIZE):
                    chunk = data[offset:offset + EVENT_SIZE]
                    if len(chunk) != EVENT_SIZE:
                        continue
                    _sec, _usec, event_type, event_code, event_value = struct.unpack(EVENT_FORMAT, chunk)
                    if event_type != EV_KEY:
                        continue
                    if event_value == KEY_PRESS and pressed is None:
                        pressed = {
                            "path": device["path"],
                            "name": device["name"],
                            "code": event_code,
                            "started_at": time.monotonic(),
                        }
                    elif pressed and device["path"] == pressed["path"] and event_code == pressed["code"]:
                        if event_value == KEY_REPEAT:
                            saw_repeat = True
                        elif event_value == KEY_RELEASE:
                            duration = time.monotonic() - pressed["started_at"]
                            suggested_mode = "hold" if saw_repeat or duration >= 0.35 else "toggle"
                            print("Detected:")
                            print(f'  input_name: "{pressed["name"]}"')
                            print(f'  input_device: "{pressed["path"]}"')
                            print(f"  code: {pressed['code']}")
                            print(f"  suggested_mode: {suggested_mode}")
                            return 0

        print("Timed out waiting for a key press", file=sys.stderr)
        return 1
    finally:
        for fd in fds:
            os.close(fd)


def notify(cfg: Config, title: str, body: str = "") -> None:
    if not cfg.notify:
        return
    args = [
        "notify-send",
        "--app-name", "简听输入",
        "--expire-time", str(cfg.notify_timeout_ms),
        title,
    ]
    if body:
        args.append(body)
    result = run_checked(args, timeout=3)
    if result.returncode != 0:
        log(f"notify-send failed: {result.stderr.strip()}")


_RUNTIME_ENGINE: dict[str, tuple[str, bool]] = {
    "cpu-onnx-llamacpp": ("CPU", False),
    "gpu-vulkan": ("CPU", True),
    "gpu-metal": ("CPU", True),
    "gpu-directml": ("Dml", True),
    "npu-openvino-linux": ("OpenVINO", True),
    "npu-openvino-qnn": ("OpenVINO", True),
    "npu-coreml": ("CPU", True),
}


def engine_settings_for_runtime(runtime_id: Optional[str]) -> tuple[str, bool]:
    """Map a UI-selected runtime candidate to Qwen3-ASR engine settings.

    Returns ``(onnx_provider, llm_use_gpu)``. When nothing is selected we keep
    the legacy default (CPU ONNX provider with GPU llama.cpp enabled) so existing
    ``config.json`` setups behave exactly as before.
    """
    if runtime_id is None:
        return ("CPU", True)
    return _RUNTIME_ENGINE.get(runtime_id, ("CPU", True))


class QwenAsr:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.backend = (cfg.asr_backend or "local").lower().strip()
        self.engine: Any = None
        self._local_loaded = False
        self._local_failed = False
        if self.backend == "local":
            self._load_local_engine()
        elif self.backend == "http":
            if not (cfg.asr_service_url or "").lower().startswith(("http://", "https://")):
                raise ValueError(
                    f"asr_backend is 'http' but asr_service_url is invalid: {cfg.asr_service_url!r}"
                )
        else:
            raise ValueError(f"asr_backend must be 'local' or 'http', got {cfg.asr_backend!r}")

    def _load_local_engine(self) -> None:
        if self._local_loaded or self._local_failed:
            return
        try:
            project_dir = Path(self.cfg.asr_project_dir)
            bin_dir = project_dir / "qwen_asr_gguf" / "inference" / "bin"

            # Make dependent .so files discoverable before qwen_asr_gguf loads libllama.
            os.environ.setdefault("GGML_VK_DISABLE_F16", "1")
            old_ld = os.environ.get("LD_LIBRARY_PATH", "")
            if str(bin_dir) not in old_ld.split(":"):
                os.environ["LD_LIBRARY_PATH"] = f"{bin_dir}:{old_ld}" if old_ld else str(bin_dir)

            sys.path.insert(0, str(project_dir))
            from qwen_asr_gguf.inference import ASREngineConfig, QwenASREngine

            onnx_provider, llm_use_gpu = engine_settings_for_runtime(self.cfg.selected_runtime_id)
            log(
                f"Loading Qwen3-ASR engine: {self.cfg.model_dir} "
                f"(runtime={self.cfg.selected_runtime_id or 'default'}, "
                f"onnx_provider={onnx_provider}, llm_use_gpu={llm_use_gpu})"
            )
            self.engine = QwenASREngine(
                config=ASREngineConfig(
                    model_dir=self.cfg.model_dir,
                    onnx_provider=onnx_provider,
                    llm_use_gpu=llm_use_gpu,
                    encoder_frontend_fn="qwen3_asr_encoder_frontend.int4.onnx",
                    encoder_backend_fn="qwen3_asr_encoder_backend.int4.onnx",
                    enable_aligner=False,
                    verbose=False,
                )
            )
            log("Qwen3-ASR engine ready")
            self._local_loaded = True
        except Exception as exc:
            self._local_failed = True
            raise ValueError(f"Failed to load local Qwen3-ASR engine: {exc!r}") from exc

    def transcribe(self, audio_path: Path) -> str:
        if self.backend == "http":
            try:
                return self._transcribe_http(audio_path)
            except Exception as exc:
                if self.cfg.asr_fallback_local:
                    log(f"HTTP ASR failed ({exc}); falling back to local engine")
                    self._load_local_engine()
                    return self._transcribe_local(audio_path)
                raise
        return self._transcribe_local(audio_path)

    def _transcribe_local(self, audio_path: Path) -> str:
        result = self.engine.transcribe(
            audio_file=str(audio_path),
            language=self.cfg.language,
            context="",
            start_second=0,
            duration=None,
            temperature=0.4,
        )
        text = (result.text or "").strip()
        if self.cfg.strip_trailing_punctuation:
            text = strip_punctuation(text)
        return text

    def _transcribe_http(self, audio_path: Path) -> str:
        url = self.cfg.asr_service_url.rstrip("/") + "/transcribe"
        with audio_path.open("rb") as handle:
            data = handle.read()
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/octet-stream"},
        )
        with urllib.request.urlopen(request, timeout=self.cfg.asr_http_timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = (payload.get("text") or "").strip()
        if self.cfg.strip_trailing_punctuation:
            text = strip_punctuation(text)
        return text

    def shutdown(self) -> None:
        if self.engine is not None:
            self.engine.shutdown()


class Recorder:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self.path: Optional[Path] = None
        self.started_at: float = 0.0

    def start(self) -> None:
        if self.proc is not None:
            return
        recordings = Path(self.cfg.recordings_dir)
        recordings.mkdir(parents=True, exist_ok=True)
        self.path = recordings / f"voice-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.wav"
        pr = self.cfg.pw_record
        args = [
            "pw-record",
            "--rate", str(pr.get("rate", 16000)),
            "--channels", str(pr.get("channels", 1)),
            "--format", str(pr.get("format", "s16")),
            str(self.path),
        ]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.started_at = time.monotonic()
        log(f"Recording started: {self.path}")

    def stop(self) -> Optional[Path]:
        if self.proc is None:
            return None
        elapsed = time.monotonic() - self.started_at
        proc = self.proc
        path = self.path
        self.proc = None
        self.path = None
        proc.send_signal(signal.SIGINT)
        try:
            _, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            _, stderr = proc.communicate(timeout=3)
        if proc.returncode not in (0, -signal.SIGINT, -signal.SIGTERM, 130, 143):
            log(f"pw-record exited with {proc.returncode}: {(stderr or '').strip()}")
        if elapsed < self.cfg.min_record_seconds:
            log(f"Recording too short ({elapsed:.2f}s), ignored")
            return None
        if path is None or not path.exists() or path.stat().st_size < 1024:
            log("Recording missing or too small, ignored")
            return None
        log(f"Recording stopped ({elapsed:.2f}s): {path}")
        return path


def insert_text(cfg: Config, text: str) -> None:
    if not text:
        log("Empty transcription, nothing to insert")
        return
    if cfg.copy_to_clipboard:
        try:
            cp = run_checked(["wl-copy"], input_text=text, timeout=2)
            if cp.returncode != 0:
                log(f"wl-copy failed: {cp.stderr.strip()}")
        except subprocess.TimeoutExpired:
            log("wl-copy timed out; continuing without clipboard copy")
    if cfg.type_text:
        if cfg.fcitx_commit:
            committed, detail = commit_text_with_fcitx(cfg, text)
            if committed:
                log(f"Committed text via fcitx: {text}")
                return
            log(f"fcitx commit failed ({detail}); falling back to {cfg.type_command}")
        try:
            typed = run_checked([cfg.type_command, text], timeout=15)
            if typed.returncode != 0:
                log(f"{cfg.type_command} failed: {typed.stderr.strip()}")
            else:
                log(f"Typed text: {text}")
        except subprocess.TimeoutExpired:
            log(f"{cfg.type_command} timed out")
    else:
        log(f"Copied text: {text}")


def iter_key_events(device: str, trigger_code: int, stop_event: Optional[threading.Event] = None):
    fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    try:
        # poll() is only a coarse 200ms wake-up so we can observe stop_event
        # promptly; actual events are read from the non-blocking fd below.
        while not stop_event or not stop_event.is_set():
            poller.poll(200)
            if stop_event and stop_event.is_set():
                break
            try:
                data = os.read(fd, EVENT_SIZE * 16)
            except BlockingIOError:
                continue
            for offset in range(0, len(data), EVENT_SIZE):
                chunk = data[offset:offset + EVENT_SIZE]
                if len(chunk) != EVENT_SIZE:
                    continue
                _sec, _usec, event_type, event_code, event_value = struct.unpack(EVENT_FORMAT, chunk)
                if event_type == EV_KEY and event_code == trigger_code:
                    yield event_value
    finally:
        os.close(fd)


def self_test(cfg: Config) -> int:
    input_device = resolve_input_device(cfg.trigger) if cfg.trigger.enabled else None
    checks = [
        (Path(cfg.asr_project_dir).exists(), f"ASR project exists: {cfg.asr_project_dir}"),
        (Path(cfg.model_dir).exists(), f"model dir exists: {cfg.model_dir}"),
        (Path(cfg.python_venv, "bin/python3").exists(), f"venv python exists: {cfg.python_venv}"),
        (subprocess.call(["which", "pw-record"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0, "pw-record available"),
        (not cfg.notify or subprocess.call(["which", "notify-send"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0, "notify-send available"),
    ]
    if cfg.type_text:
        checks.append((
            subprocess.call(["which", cfg.type_command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0,
            f"{cfg.type_command} available",
        ))
    if cfg.copy_to_clipboard:
        checks.append((
            subprocess.call(["which", "wl-copy"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0,
            "wl-copy available",
        ))
    if cfg.fcitx_commit:
        ok, detail = ping_fcitx(cfg)
        checks.append((ok, f"fcitx addon ping: {detail}"))
    if cfg.trigger.enabled:
        checks.insert(0, (input_device is not None, "trigger input device configured"))
        if input_device:
            checks.insert(1, (Path(input_device).exists(), f"input device exists: {input_device}"))
            checks.insert(2, (os.access(input_device, os.R_OK), f"input device readable: {input_device}"))
        checks.insert(3, (cfg.trigger.code is not None, f"trigger key code configured: {cfg.trigger.code}"))
    else:
        checks.insert(0, (True, "trigger disabled by default"))
    ok = True
    for passed, name in checks:
        print(("✅" if passed else "❌"), name)
        ok = ok and passed
    return 0 if ok else 1


def transcribe_file(cfg: Config, audio: Path, *, do_type: bool) -> int:
    asr = QwenAsr(cfg)
    try:
        text = asr.transcribe(audio)
        print(text)
        if do_type:
            insert_text(cfg, text)
    finally:
        asr.shutdown()
    return 0


class TranscribeWorker:
    def __init__(self, asr: QwenAsr, cfg: Config) -> None:
        self.asr = asr
        self.cfg = cfg
        self._queue: "queue.Queue[Optional[Path]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="voxkey-transcribe", daemon=True)
        self._thread.start()

    def submit(self, audio: Path) -> None:
        self._queue.put(audio)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                self._process(item)
            finally:
                self._queue.task_done()

    def _process(self, audio: Path) -> None:
        try:
            notify(self.cfg, "正在转写…")
            text = self.asr.transcribe(audio)
            log(f"Transcribed: {text}")
            notify(self.cfg, "语音输入完成", text[:80] if text else "未识别到文字")
            insert_text(self.cfg, text)
        except Exception as exc:
            log(f"Transcription failed: {exc!r}")
            notify(self.cfg, "语音输入失败", repr(exc)[:120])

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=10)


def _finish_recording(recorder: Recorder, worker: TranscribeWorker, cfg: Config) -> None:
    audio = recorder.stop()
    if audio is None:
        notify(cfg, "语音输入已取消", "录音太短或没有音频")
        return
    worker.submit(audio)


def run_daemon(cfg: Config) -> int:
    trigger = cfg.trigger
    if not trigger.enabled:
        log("No voice input key is configured; trigger.enabled is false.")
        log("Run --list-devices and --detect-key, then enable trigger in config.json.")
        return 0
    if trigger.backend != "evdev":
        log(f"Unsupported trigger backend={trigger.backend!r}; expected 'evdev'")
        return 2
    if trigger.code is None:
        log("No trigger key code configured.")
        return 2

    input_device = resolve_input_device(trigger)
    if not input_device:
        log("No trigger input device configured.")
        return 2
    if not Path(input_device).exists():
        log(f"Trigger input device does not exist: {input_device}")
        return 2
    if not os.access(input_device, os.R_OK):
        log(f"Trigger input device is not readable: {input_device}")
        return 2
    trigger_mode = trigger.mode.lower().strip()
    if trigger_mode not in {"hold", "toggle"}:
        log(f"Unsupported trigger.mode={trigger.mode!r}; expected 'hold' or 'toggle'")
        return 2

    recorder = Recorder(cfg)
    asr = QwenAsr(cfg)
    worker = TranscribeWorker(asr, cfg)
    stop_event = threading.Event()

    def _request_stop(signum: int, _frame: object) -> None:
        log(f"Received signal {signum}; shutting down")
        stop_event.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    log(
        f"Listening for {trigger.name} code={trigger.code} "
        f"mode={trigger_mode} on {input_device}"
    )
    try:
        for state in iter_key_events(input_device, trigger.code, stop_event):
            log(f"Trigger event: {key_state_name(state)} ({state})")
            if trigger_mode == "hold":
                if state in {KEY_PRESS, KEY_REPEAT}:
                    if recorder.proc is None:
                        recorder.start()
                        notify(cfg, "正在录音…", "松开按键后转写")
                elif state == KEY_RELEASE:
                    notify(cfg, "录音结束", "开始转写")
                    _finish_recording(recorder, worker, cfg)
            elif trigger_mode == "toggle" and state == KEY_PRESS:
                if recorder.proc is None:
                    recorder.start()
                    notify(cfg, "正在录音…", "再次按键后转写")
                else:
                    notify(cfg, "录音结束", "开始转写")
                    _finish_recording(recorder, worker, cfg)
    finally:
        stop_event.set()
        if recorder.proc is not None:
            recorder.stop()
        worker.stop()
        asr.shutdown()
    return 0


def main() -> int:
    default_config = Path(__file__).resolve().with_name("config.json")
    parser = argparse.ArgumentParser(description="VoxKey push-to-talk voice input daemon")
    parser.add_argument("--config", default=os.environ.get("QWEN_VOICE_INPUT_CONFIG", str(default_config)))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--ping-fcitx", action="store_true")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--detect-key", nargs="?", const="", metavar="DEVICE")
    parser.add_argument("--detect-timeout", type=float, default=15.0)
    parser.add_argument("--transcribe-file", type=Path)
    parser.add_argument("--type", action="store_true", help="type transcribe-file result with wtype")
    args = parser.parse_args()

    if args.list_devices:
        return print_input_devices()
    if args.detect_key is not None:
        return detect_key(args.detect_key or None, timeout=args.detect_timeout)

    try:
        cfg = load_config(Path(args.config))
    except (OSError, ValueError) as exc:
        print(f"Failed to load config: {exc}", file=sys.stderr)
        return 2

    if cfg.python_venv and not sys.executable.startswith(str(cfg.python_venv)):
        log(
            f"WARNING: running with {sys.executable}, but config.python_venv is "
            f"{cfg.python_venv}; model/engine dependencies for that venv may be missing."
        )
    if args.self_test:
        return self_test(cfg)
    if args.ping_fcitx:
        ok, detail = ping_fcitx(cfg)
        print(detail)
        return 0 if ok else 1
    if args.transcribe_file:
        return transcribe_file(cfg, args.transcribe_file, do_type=args.type)
    return run_daemon(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
