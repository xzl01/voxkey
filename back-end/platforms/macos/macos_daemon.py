# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""macOS push-to-talk voice input daemon — dual-engine (NCE + GPU).

Flow:
  * A global hotkey (hotkey_helper.swift) starts/stops recording.
  * While held, the mic (capture_helper.swift) streams 16 kHz mono PCM; we run
    the FunASR Core ML engine on the Neural Engine for live partials.
  * On release we run the full dual-engine transcription (FunASR NCE fast +
    Qwen3-ASR GPU refined) and commit the fused text via clipboard + paste, so
    Chinese text lands correctly regardless of the active IME.

This is the macOS counterpart of back-end/voice-daemon/voice_input_daemon.py.
Run:
  python macos_daemon.py --config back-end/platforms/macos/config.json
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import threading
from pathlib import Path

import numpy as np

from audio import MicCapture
from common import resolve_asset
from orchestrator import DualEngineOrchestrator, FusionConfig

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
logger = logging.getLogger("voxkey.daemon")

HELPERS = Path(__file__).resolve().parent


def load_config(path: Path) -> dict:
    """Load the daemon configuration (JSON) from ``path``."""
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Text commit (clipboard + paste is IME-safe for CJK on macOS)
# --------------------------------------------------------------------------- #
def commit_text(text: str, *, notify: bool = True) -> None:
    """Copy ``text`` to the clipboard and paste it (IME-safe on macOS); optional notification."""
    if not text:
        logger.info("Empty transcription; nothing to commit")
        return
    p = subprocess.run(["pbcopy"], input=text, text=True, check=False)
    if p.returncode != 0:
        logger.warning("pbcopy failed")
    # paste via System Events (Cmd+V)
    script = 'tell application "System Events" to keystroke "v" using command down'
    pe = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if pe.returncode != 0:
        logger.warning("paste failed: %s", pe.stderr.strip())
    else:
        logger.info("Committed: %s", text)
    if notify:
        notify_mac(text)


def _applescript_escape(value: str) -> str:
    """Escape a value for safe embedding inside an AppleScript double-quoted
    literal. Prevents quote-breaking and script injection from (possibly remote)
    transcription text."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )


def notify_mac(text: str, title: str = "简听输入") -> None:
    script = (
        f'display notification "{_applescript_escape(text[:200])}" '
        f'with title "{_applescript_escape(title)}"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True, check=False)


# --------------------------------------------------------------------------- #
# Hotkey + capture state machine
# --------------------------------------------------------------------------- #
class Daemon:
    """Push-to-talk voice input daemon: hotkey capture -> live NCE partials -> fused commit."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.orch = self._build_orchestrator()
        self.capture: MicCapture | None = None
        self.recording = False
        self.buffer: list[np.ndarray] = []
        self.live_thread: threading.Thread | None = None

    def _build_orchestrator(self) -> DualEngineOrchestrator:
        base = HELPERS
        eng = self.cfg.get("engines", {})
        fusion = self.cfg.get("fusion", {})
        funasr = qwen3 = None
        if eng.get("funasr_coreml", {}).get("enabled", True):
            from funasr_coreml import FunASRCoreML

            fc = eng["funasr_coreml"]
            funasr = FunASRCoreML(
                str((base / fc["model_path"]).expanduser()),
                compute_units=fc.get("compute_units", "ane"),
                timeout_s=fc.get("timeout_s", 8.0),
            )
            funasr.warmup()
        if eng.get("qwen3_gpu", {}).get("enabled", True):
            from qwen3_gpu import Qwen3GPU

            qc = eng["qwen3_gpu"]
            qwen3 = Qwen3GPU(
                model_dir=str((base / qc["model_dir"]).expanduser()),
                project_dir=str(resolve_asset(HELPERS, self.cfg["asr_project_dir"]))
                if self.cfg.get("asr_project_dir")
                else None,
                onnx_provider=qc.get("onnx_provider", "CPU"),
                llm_use_gpu=qc.get("llm_use_gpu", True),
                encoder_frontend_fn=qc.get(
                    "encoder_frontend_fn", "qwen3_asr_encoder_frontend.int4.onnx"
                ),
                encoder_backend_fn=qc.get(
                    "encoder_backend_fn", "qwen3_asr_encoder_backend.int4.onnx"
                ),
                llm_fn=qc.get("llm_fn", "qwen3_asr_llm.q4_k.gguf"),
                enable_aligner=qc.get("enable_aligner", False),
                timeout_s=qc.get("timeout_s", 20.0),
            )
            qwen3.warmup()
        return DualEngineOrchestrator(
            funasr,
            qwen3,
            FusionConfig(
                mode=fusion.get("mode", "fast_first"),
                funasr_priority_until_s=fusion.get("funasr_priority_until_s", 1.2),
            ),
        )

    def start_recording(self) -> None:
        """Begin mic capture and spawn the live NCE partial-recognition thread."""
        if self.recording:
            return
        self.capture = MicCapture()
        self.capture.start()
        self.buffer = []
        self.recording = True
        logger.info("Recording started")
        self.live_thread = threading.Thread(target=self._live_loop, daemon=True)
        self.live_thread.start()

    def _live_loop(self) -> None:
        assert self.capture
        for chunk in self.capture.iter_chunks(chunk_seconds=0.8):
            if not self.recording:
                break
            self.buffer.append(chunk)
            partial = np.concatenate(self.buffer)
            if self.orch.funasr and len(partial) > 16_000 * 0.4:
                tr = self.orch.funasr.transcribe(partial)
                if tr.text.strip():
                    logger.info("[live/NCE] %s", tr.text)
                    notify_mac(tr.text, title="识别中…")

    def stop_and_transcribe(self) -> None:
        """Stop capture, run the full dual-engine transcription, and commit the result."""
        if not self.recording:
            return
        self.recording = False
        assert self.capture
        self.capture.stop()
        if self.live_thread:
            self.live_thread.join(timeout=2)
        full = np.concatenate(self.buffer) if self.buffer else np.zeros(0, dtype=np.float32)
        if len(full) < 16_000 * self.cfg.get("min_record_seconds", 0.3):
            logger.info("Recording too short; ignored")
            return
        logger.info("Transcribing (%d samples)…", len(full))
        result = self.orch.transcribe(full)
        logger.info("Final (%s, %.2fs): %s", result.chosen.value, result.total_s, result.final_text)
        commit_text(result.final_text, notify=self.cfg.get("notify", True))

    def run(self) -> int:
        """Start the hotkey listener loop and dispatch record/stop events until exit."""
        helper = HELPERS / "hotkey_helper"
        if not helper.exists():
            logger.error(
                "hotkey_helper not built. Run: swiftc -O hotkey_helper.swift -o hotkey_helper"
            )
            return 2
        key = self.cfg.get("trigger", {}).get("code")
        args = [str(helper)] + (["--key", f"{key:02x}"] if isinstance(key, int) else [])
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        logger.info("Hotkey listener started (press the configured key to record)")
        try:
            assert proc.stdout
            for line in proc.stdout:
                line = line.strip()
                if line == "down":
                    self.start_recording()
                elif line == "up":
                    self.stop_and_transcribe()
        finally:
            proc.terminate()
            if self.orch.funasr:
                self.orch.funasr.shutdown()
            if self.orch.qwen3:
                self.orch.qwen3.shutdown()
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HELPERS / "config.json"))
    args = ap.parse_args()
    cfg = load_config(Path(args.config).expanduser())
    return Daemon(cfg).run()


if __name__ == "__main__":
    raise SystemExit(main())
