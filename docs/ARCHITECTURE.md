# VoxKey Cross-Platform Architecture

简听输入 (VoxKey) is moving from a Linux/Wayland prototype to a
cross-platform desktop application. The user-facing product is a packaged
native app built with Tauri; React/Vite is only the renderer technology inside
the app, not a web deployment target. The first install should contain only the
desktop UI and lightweight service management. Model files and compute runtimes
are selected and installed after first launch.

## Modules

```text
Desktop UI
  Tauri native shell + React + TypeScript renderer

Core
  Shared configuration
  Runtime detection
  Model inventory
  Service lifecycle

Platform adapters
  Windows: hotkey, capture, text injection, notification, autostart
  Linux: evdev/portal, PipeWire, fcitx/wtype, notification, systemd user
  macOS: hotkey, AVFoundation capture, paste/text injection, notification, launchd

ASR service
  Stable local API
  CPU/GPU/NPU backend loading
  Model download and verification
  Benchmark and diagnostics
```

## Runtime Model

User-facing options stay simple:

- CPU
- GPU
- NPU

Internally each option maps to a concrete runtime:

| Platform | CPU | GPU | NPU |
| --- | --- | --- | --- |
| Windows | ONNX Runtime + llama.cpp CPU | DirectML / llama.cpp | OpenVINO NPU or QNN |
| Linux | ONNX Runtime + llama.cpp CPU | Vulkan / llama.cpp | OpenVINO or vendor runtime |
| macOS | ONNX Runtime + llama.cpp CPU | Metal / llama.cpp | Core ML / ANE experimental |

CPU is the compatibility baseline. GPU and NPU are installed only when the
machine has a compatible runtime.

## Current Scaffold

The repo is split into `front-end/` (user-facing app) and `back-end/`
(services, daemons, shared libs), with OS-specific artifacts isolated under
`*/platforms/{linux,macos,windows}`.

- `front-end/desktop-ui`: cross-platform Tauri shell; writes user selections
  (ASR backend, service URL, fallback, timeout, compute runtime) into Tauri's
  `settings.json`. OS-specific Tauri configs/build scripts live in
  `front-end/desktop-ui/src-tauri/platforms/`.
- `back-end/core`: shared Rust types and runtime candidate detection
  (crate `voxkey-core`).
- `back-end/asr-service`: local service boundary for ASR backends. The
  `/transcribe` endpoint is a placeholder (returns 501) until the Qwen3-ASR
  backend is wired in.
- `back-end/voice-daemon/voice_input_daemon.py`: existing Linux prototype. On
  startup it reads `config.json` (default: `back-end/platforms/linux/`) and
  then overlays the desktop UI's Tauri `settings.json` (`apply_ui_settings`),
  so the GUI is the single source of truth for ASR backend and compute runtime
  on the same machine. `selected_runtime_id` drives the local engine's ONNX
  provider / GPU path. Override the Tauri settings location with
  `ui_settings_path` in `config.json`. The Linux launcher, service, fcitx5
  addon, and example config live under `back-end/platforms/linux/`.

## Near-Term Milestones

1. Keep the current Linux prototype working.
2. Make the desktop UI show platform/runtime candidates.
3. Add model and runtime download manifests.
4. Port file transcription through the ASR service using CPU first.
5. Add platform adapters for recording, hotkeys, and text commit.
