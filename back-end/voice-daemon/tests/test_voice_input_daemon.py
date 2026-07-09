# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT

import http.server
import io
import json
import os
import struct
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import voice_input_daemon as daemon  # noqa: E402


PROC_INPUT_DEVICES = """\
I: Bus=0011 Vendor=0001 Product=0001 Version=ab41
N: Name="AT Translated Set 2 keyboard"
P: Phys=isa0060/serio0/input0
H: Handlers=sysrq kbd event3 leds
B: PROP=0

I: Bus=0019 Vendor=0000 Product=0000 Version=0000
N: Name="Ideapad extra buttons"
P: Phys=ideapad/input0
H: Handlers=kbd event6
B: PROP=0

I: Bus=0003 Vendor=0001 Product=0001 Version=0001
N: Name="keyd virtual keyboard"
P: Phys=keyd/input0
H: Handlers=sysrq kbd event13 leds
B: PROP=0
"""


def make_config(**overrides):
    values = {
        "trigger": daemon.TriggerConfig(),
        "recordings_dir": "/tmp/qwen-recordings",
        "asr_project_dir": "/tmp/qwen-asr",
        "model_dir": "/tmp/qwen-model",
        "python_venv": "/tmp/qwen-venv",
        "language": "Chinese",
        "min_record_seconds": 0.25,
        "pw_record": {"rate": 16000, "channels": 1, "format": "s16"},
        "type_command": "wtype",
        "copy_to_clipboard": False,
        "type_text": True,
        "notify": False,
        "notify_timeout_ms": 1200,
        "strip_trailing_punctuation": False,
        "fcitx_commit": False,
        "fcitx_socket": None,
        "fcitx_commit_timeout_ms": 500,
    }
    values.update(overrides)
    return daemon.Config(**values)


def write_config(path: Path, data: dict):
    path.write_text(json.dumps(data), encoding="utf-8")


def minimal_raw_config(**overrides):
    data = {
        "trigger": {"enabled": False},
        "recordings_dir": "$HOME/.local/share/voxkey/recordings",
        "asr_project_dir": "$HOME/AI/Model/Qwen3-ASR-GGUF",
        "model_dir": "model-1.7B",
        "python_venv": "$HOME/qwen3-asr-venv",
        "language": "Chinese",
        "min_record_seconds": 0.25,
        "pw_record": {"rate": 16000, "channels": 1, "format": "s16"},
        "type_command": "wtype",
        "copy_to_clipboard": False,
        "type_text": True,
        "fcitx_commit": False,
        "fcitx_socket": None,
        "fcitx_commit_timeout_ms": 500,
        "notify": False,
        "notify_timeout_ms": 1200,
        "strip_trailing_punctuation": False,
    }
    data.update(overrides)
    return data


class ConfigLoadingTests(unittest.TestCase):
    def test_new_trigger_config_defaults_disabled_and_expands_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            write_config(config_path, minimal_raw_config())

            with mock.patch.dict(os.environ, {"HOME": "/home/tester"}):
                cfg = daemon.load_config(config_path)

            self.assertFalse(cfg.trigger.enabled)
            self.assertEqual(cfg.trigger.backend, "evdev")
            self.assertIsNone(cfg.trigger.input_device)
            self.assertIsNone(cfg.trigger.code)
            self.assertEqual(cfg.recordings_dir, "/home/tester/.local/share/voxkey/recordings")
            self.assertEqual(cfg.asr_project_dir, "/home/tester/AI/Model/Qwen3-ASR-GGUF")
            self.assertEqual(cfg.python_venv, "/home/tester/qwen3-asr-venv")
            self.assertEqual(cfg.model_dir, str(config_path.parent / "model-1.7B"))

    def test_legacy_trigger_fields_are_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            raw = minimal_raw_config(
                trigger_code=193,
                trigger_name="legacy voice key",
                trigger_mode="hold",
                input_name="keyd virtual keyboard",
                input_device="/dev/input/event13",
            )
            raw.pop("trigger")
            write_config(config_path, raw)

            cfg = daemon.load_config(config_path)

            self.assertTrue(cfg.trigger.enabled)
            self.assertEqual(cfg.trigger.backend, "evdev")
            self.assertEqual(cfg.trigger.input_name, "keyd virtual keyboard")
            self.assertEqual(cfg.trigger.input_device, "/dev/input/event13")
            self.assertEqual(cfg.trigger.code, 193)
            self.assertEqual(cfg.trigger.name, "legacy voice key")
            self.assertEqual(cfg.trigger.mode, "hold")


class InputDeviceTests(unittest.TestCase):
    def test_iter_input_devices_parses_proc_input_devices(self):
        with mock.patch.object(daemon.Path, "read_text", return_value=PROC_INPUT_DEVICES):
            devices = daemon.iter_input_devices()

        self.assertEqual(
            devices,
            [
                {
                    "path": "/dev/input/event3",
                    "name": "AT Translated Set 2 keyboard",
                    "handlers": "sysrq kbd event3 leds",
                },
                {
                    "path": "/dev/input/event6",
                    "name": "Ideapad extra buttons",
                    "handlers": "kbd event6",
                },
                {
                    "path": "/dev/input/event13",
                    "name": "keyd virtual keyboard",
                    "handlers": "sysrq kbd event13 leds",
                },
            ],
        )

    def test_find_input_device_by_exact_or_partial_name(self):
        with mock.patch.object(daemon.Path, "read_text", return_value=PROC_INPUT_DEVICES):
            self.assertEqual(daemon.find_input_device_by_name("keyd virtual keyboard"), "/dev/input/event13")
            self.assertEqual(daemon.find_input_device_by_name("Ideapad"), "/dev/input/event6")
            self.assertIsNone(daemon.find_input_device_by_name("not present"))

    def test_print_input_devices_marks_readability(self):
        devices = [
            {"path": "/dev/input/event3", "name": "Keyboard", "handlers": "kbd event3"},
            {"path": "/dev/input/event6", "name": "Extra Buttons", "handlers": "kbd event6"},
        ]

        def fake_access(path, mode):
            return path == "/dev/input/event6"

        out = io.StringIO()
        with (
            mock.patch.object(daemon, "iter_input_devices", return_value=devices),
            mock.patch.object(daemon.os, "access", side_effect=fake_access),
            redirect_stdout(out),
        ):
            code = daemon.print_input_devices()

        self.assertEqual(code, 0)
        self.assertIn("/dev/input/event3\tno-read-access\tKeyboard", out.getvalue())
        self.assertIn("/dev/input/event6\treadable\tExtra Buttons", out.getvalue())


class DetectKeyTests(unittest.TestCase):
    def pack_key_event(self, code, value):
        return struct.pack(daemon.EVENT_FORMAT, 0, 0, daemon.EV_KEY, code, value)

    def test_detect_key_reports_pressed_key_and_hold_mode(self):
        class FakePoll:
            def register(self, fd, mask):
                self.fd = fd

            def poll(self, timeout):
                return [(self.fd, 1)]

        events = b"".join([
            self.pack_key_event(193, daemon.KEY_PRESS),
            self.pack_key_event(193, daemon.KEY_REPEAT),
            self.pack_key_event(193, daemon.KEY_RELEASE),
        ])
        devices = [{"path": "/dev/input/event13", "name": "keyd virtual keyboard", "handlers": "kbd event13"}]

        out = io.StringIO()
        with (
            mock.patch.object(daemon, "iter_input_devices", return_value=devices),
            mock.patch.object(daemon.os, "open", return_value=42),
            mock.patch.object(daemon.os, "read", return_value=events),
            mock.patch.object(daemon.os, "close"),
            mock.patch.object(daemon.select, "poll", return_value=FakePoll()),
            redirect_stdout(out),
        ):
            code = daemon.detect_key(None, timeout=1)

        self.assertEqual(code, 0)
        self.assertIn('input_name: "keyd virtual keyboard"', out.getvalue())
        self.assertIn('input_device: "/dev/input/event13"', out.getvalue())
        self.assertIn("code: 193", out.getvalue())
        self.assertIn("suggested_mode: hold", out.getvalue())

    def test_detect_key_fails_when_no_devices_are_readable(self):
        devices = [{"path": "/dev/input/event13", "name": "keyd virtual keyboard", "handlers": "kbd event13"}]
        err = io.StringIO()

        with (
            mock.patch.object(daemon, "iter_input_devices", return_value=devices),
            mock.patch.object(daemon.os, "open", side_effect=PermissionError),
            redirect_stderr(err),
        ):
            code = daemon.detect_key(None, timeout=1)

        self.assertEqual(code, 1)
        self.assertIn("No readable input devices", err.getvalue())


class DaemonRunTests(unittest.TestCase):
    def test_disabled_trigger_does_not_load_asr_or_listen(self):
        cfg = make_config(trigger=daemon.TriggerConfig(enabled=False))

        with (
            mock.patch.object(daemon, "QwenAsr") as asr,
            mock.patch.object(daemon, "iter_key_events") as key_events,
        ):
            code = daemon.run_daemon(cfg)

        self.assertEqual(code, 0)
        asr.assert_not_called()
        key_events.assert_not_called()

    def test_enabled_trigger_missing_device_fails_before_loading_asr(self):
        cfg = make_config(
            trigger=daemon.TriggerConfig(
                enabled=True,
                input_device="/tmp/voxkey-missing-event",
                code=193,
            )
        )

        with mock.patch.object(daemon, "QwenAsr") as asr:
            code = daemon.run_daemon(cfg)

        self.assertEqual(code, 2)
        asr.assert_not_called()


class SelfTestTests(unittest.TestCase):
    def test_self_test_disabled_trigger_skips_input_device_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            asr_project = base / "asr"
            model = base / "model"
            venv_python = base / "venv" / "bin" / "python3"
            asr_project.mkdir()
            model.mkdir()
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("#!/bin/sh\n", encoding="utf-8")

            cfg = make_config(
                trigger=daemon.TriggerConfig(enabled=False),
                asr_project_dir=str(asr_project),
                model_dir=str(model),
                python_venv=str(base / "venv"),
                type_text=False,
                notify=False,
                fcitx_commit=False,
            )

            out = io.StringIO()
            with (
                mock.patch.object(daemon.subprocess, "call", return_value=0),
                redirect_stdout(out),
            ):
                code = daemon.self_test(cfg)

        self.assertEqual(code, 0)
        self.assertIn("trigger disabled by default", out.getvalue())
        self.assertNotIn("input device readable", out.getvalue())


class InsertTextTests(unittest.TestCase):
    def test_fcitx_success_skips_wtype_fallback(self):
        cfg = make_config(fcitx_commit=True, type_text=True)

        with (
            mock.patch.object(daemon, "commit_text_with_fcitx", return_value=(True, "OK")) as commit,
            mock.patch.object(daemon, "run_checked") as run_checked,
        ):
            daemon.insert_text(cfg, "hello")

        commit.assert_called_once_with(cfg, "hello")
        run_checked.assert_not_called()

    def test_fcitx_failure_falls_back_to_wtype(self):
        cfg = make_config(fcitx_commit=True, type_text=True, type_command="wtype")
        completed = subprocess.CompletedProcess(["wtype", "hello"], 0, "", "")

        with (
            mock.patch.object(daemon, "commit_text_with_fcitx", return_value=(False, "ERR no-focus")),
            mock.patch.object(daemon, "run_checked", return_value=completed) as run_checked,
        ):
            daemon.insert_text(cfg, "hello")

        run_checked.assert_called_once_with(["wtype", "hello"], timeout=15)

    def test_wl_copy_timeout_does_not_block_typing(self):
        cfg = make_config(copy_to_clipboard=True, fcitx_commit=False, type_text=True, type_command="wtype")
        typed = subprocess.CompletedProcess(["wtype", "hello"], 0, "", "")

        with mock.patch.object(
            daemon,
            "run_checked",
            side_effect=[subprocess.TimeoutExpired(["wl-copy"], 2), typed],
        ) as run_checked:
            daemon.insert_text(cfg, "hello")

        self.assertEqual(run_checked.call_args_list[0], mock.call(["wl-copy"], input_text="hello", timeout=2))
        self.assertEqual(run_checked.call_args_list[1], mock.call(["wtype", "hello"], timeout=15))


class AsrBackendTests(unittest.TestCase):
    def test_invalid_backend_is_rejected_by_validate(self):
        with self.assertRaises(ValueError):
            daemon.validate_config(make_config(asr_backend="bogus"))

    def test_http_backend_requires_valid_url(self):
        with self.assertRaises(ValueError):
            daemon.validate_config(make_config(asr_backend="http", asr_service_url="not-a-url"))

    def test_http_backend_accepts_only_http_scheme(self):
        with self.assertRaises(ValueError):
            daemon.validate_config(make_config(asr_backend="http", asr_service_url="ftp://x"))

    def test_http_backend_transcribes_via_service(self):
        received = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received["bytes"] = self.rfile.read(length)
                body = json.dumps({"text": "hello from service"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
                tmp.write(b"RIFF....wav-data")
                tmp.flush()
                cfg = make_config(
                    asr_backend="http",
                    asr_service_url=f"http://127.0.0.1:{port}",
                    asr_fallback_local=False,
                )
                asr = daemon.QwenAsr(cfg)
                text = asr.transcribe(Path(tmp.name))
            self.assertEqual(text, "hello from service")
            self.assertGreater(len(received.get("bytes", b"")), 0)
        finally:
            server.shutdown()

    def test_http_backend_without_fallback_propagates_errors(self):
        cfg = make_config(
            asr_backend="http",
            asr_service_url="http://127.0.0.1:1",
            asr_fallback_local=False,
        )
        asr = daemon.QwenAsr(cfg)
        with self.assertRaises(Exception):
            asr.transcribe(Path("/nonexistent-voxkey.wav"))


if __name__ == "__main__":
    unittest.main()
