# VoxKey Roadmap

Last updated: 2026-06-30

This roadmap is the collaboration plan for moving 简听输入 (VoxKey) from the
current Linux prototype into a cross-platform desktop app. Keep
implementation-specific details in `docs/ARCHITECTURE.md`; use this file to
track product phases, module ownership, and open decisions.

## Product Direction

- Ship a native desktop app, not a browser-hosted product.
- Keep the first installer lightweight: desktop UI, configuration, permission
  onboarding, runtime detection, and service management only.
- Install models and compute runtimes after first launch based on the user's
  hardware choice.
- Support the same user-facing runtime choices on every platform: CPU, GPU, NPU.
- Keep CPU as the compatibility baseline before optimizing GPU or NPU paths.
- Preserve the existing Linux prototype until the new app has feature parity.

## Phase Plan

### Phase 0: Repository Foundation

Status: done

Deliverables:

- Rename product identity to 简听输入 / VoxKey.
- Add the Tauri desktop shell under `apps/desktop-ui`.
- Add the shared Rust crate under `crates/voxkey-core`.
- Add the ASR service boundary under `services/asr-service`.
- Keep the existing Linux daemon, fcitx5 addon, and systemd service working.

Exit criteria:

- `pnpm typecheck`, `pnpm build`, `cargo check --workspace`, and the Linux
  daemon unit tests pass.
- macOS desktop bundle can be built.

### Phase 1: Desktop App Shell And Runtime Selection

Status: current

Deliverables:

- First-launch flow for language, microphone permission, text-input permission,
  and runtime selection.
- Runtime cards for CPU, GPU, and NPU with platform-specific availability.
- Local configuration persistence through the Tauri app.
- Hardware scan result surfaced in the UI.
- Diagnostics panel with platform, model path, runtime path, and service status.
- Clear separation between product app launch and renderer-only web preview.

Exit criteria:

- A user can install the app, open it, see available compute choices, and save a
  runtime preference without installing a model yet.
- Unsupported runtimes explain why they are unavailable.

Iteration 1, 2026-06-30:

- Added Tauri commands for loading settings, saving selected runtime, and
  probing the local ASR service health endpoint.
- Added desktop UI state for saved runtime preference and ASR service status.
- Added `DesktopSettings` in `crates/voxkey-core` with tests for default local
  service configuration.

### Phase 2: CPU Baseline End-To-End

Status: next

Deliverables:

- Stable local ASR service API for health, model inventory, runtime inventory,
  benchmark, and transcription.
- Model manager for download, checksum verification, install, update, and
  removal.
- CPU transcription path working from a selected audio file.
- CPU transcription path working from microphone capture.
- macOS Apple Silicon CPU path verified first, then Windows and Linux.
- Existing Linux prototype regression-tested during the transition.

Exit criteria:

- On a machine with no GPU or NPU acceleration configured, the app can record,
  transcribe, and return text locally.
- Missing model/runtime states are recoverable from the UI.

### Phase 3: Platform Input Pipeline

Status: next

Deliverables:

- Cross-platform trigger abstraction for hold-to-talk and toggle-to-talk.
- Cross-platform audio capture abstraction.
- Cross-platform text commit abstraction.
- Permission and recovery UX for blocked microphone, accessibility, input
  monitoring, or desktop portal states.

Platform targets:

| Platform | Trigger | Audio | Text commit |
| --- | --- | --- | --- |
| macOS | Global hotkey / event tap | AVFoundation | Accessibility or pasteboard fallback |
| Windows | Global hotkey | WASAPI | UI Automation, TSF, or clipboard fallback |
| Linux | Desktop portal / evdev fallback | PipeWire | fcitx5, portal, or wtype fallback |

Exit criteria:

- Each platform has one documented default path and one fallback path.
- Text submission does not require users to paste manually in the normal case.

### Phase 4: Optional GPU And NPU Packages

Status: later

Deliverables:

- Backend package manifest format for optional runtime downloads.
- GPU backend candidates:
  - macOS: Metal path where the ASR engine supports it.
  - Windows: DirectML or Vulkan path.
  - Linux: Vulkan path.
- NPU backend candidates:
  - macOS: Core ML / ANE experiment if model conversion is practical.
  - Windows: OpenVINO NPU, QNN, or vendor runtime depending on hardware.
  - Linux: OpenVINO NPU or vendor runtime depending on hardware.
- Benchmark flow that compares CPU/GPU/NPU before making recommendations.
- Compatibility database keyed by platform, architecture, accelerator, runtime,
  model, and known failure mode.

Exit criteria:

- GPU/NPU options are only shown as installable when the app can explain the
  required runtime and support status.
- The user can switch back to CPU if an accelerated backend fails.

### Phase 5: Packaging, Updates, And Distribution

Status: later

Deliverables:

- macOS `.app` and `.dmg`, with signing and notarization plan.
- Windows installer, with code-signing plan.
- Linux AppImage, deb, rpm, or distro package plan.
- Runtime/model cache directory policy per platform.
- Optional backend package update flow independent from UI updates.
- Crash logs and diagnostics export that do not include user audio by default.

Exit criteria:

- The UI installer stays small.
- Backend/model downloads can be resumed, verified, removed, and updated.

### Phase 6: Reliability, Privacy, And Release Quality

Status: later

Deliverables:

- Automated tests for config, runtime detection, service API, and platform
  adapters.
- Smoke tests for packaging on macOS, Windows, and Linux.
- Permission-denied test cases for microphone and text commit.
- Local-only privacy policy and explicit data retention behavior.
- Release checklist covering model licenses, third-party runtime licenses, and
  binary redistribution rules.

Exit criteria:

- A release candidate can be validated without relying on the developer's local
  environment.
- Runtime errors lead to actionable diagnostics instead of silent failure.

## Module Backlog

### Desktop UI

- First-launch setup wizard.
- Runtime selection cards.
- Model download and install manager.
- Recording state and transcription state.
- Permissions and diagnostics views.
- Tray/menu-bar controls.
- Settings for trigger mode, language, model, and output behavior.

### Core

- Shared config schema and migration path.
- Runtime candidate detection.
- Model inventory and checksum metadata.
- Backend capability model.
- Service lifecycle manager.
- Diagnostics bundle generator.

### ASR Service

- Health and readiness endpoints.
- Runtime and model inventory endpoints.
- Transcribe-file API.
- Streaming microphone API.
- Benchmark API.
- Structured error format that can be shown directly in the UI.

### Backend Adapters

- CPU adapter as the baseline.
- GPU adapters behind explicit capability checks.
- NPU adapters behind explicit capability checks.
- Per-backend install, verify, update, and uninstall hooks.
- Per-backend benchmark metadata.

### Platform Adapters

- macOS: microphone permission, accessibility permission, global shortcut,
  capture, text commit, launch agent.
- Windows: microphone permission, global shortcut, capture, text commit,
  installer, autostart.
- Linux: PipeWire/portal handling, evdev fallback, fcitx5/wtype commit,
  systemd user service, desktop autostart.

### Existing Linux Prototype

- Keep `voice_input_daemon.py` usable while the new desktop app is incomplete.
- Avoid breaking current fcitx5 addon behavior during renames.
- Fold proven Linux behavior into the new platform adapter layer gradually.

## Backend Strategy

| User option | Role | First target | Packaging rule |
| --- | --- | --- | --- |
| CPU | Compatibility baseline | Apple Silicon macOS, then Windows and Linux | Safe default, always available when model dependencies exist |
| GPU | Performance option | Linux Vulkan and macOS Metal feasibility checks | Optional package, shown only after hardware/runtime detection |
| NPU | Power-efficient option | OpenVINO/QNN/Core ML experiments | Experimental until conversion, accuracy, and latency are proven |

Do not commit model weights, converted model files, runtime build artifacts, or
downloaded accelerator SDKs into this repository.

## Collaboration Rules

- New cross-platform work should live under `apps/`, `crates/`, or `services/`.
- Existing Linux prototype fixes can stay in the current daemon and fcitx5
  paths until the replacement path is ready.
- Each feature should identify the phase it supports.
- Platform-specific behavior must be documented next to the implementation or
  linked from this roadmap.
- UI work should treat Tauri as the product shell; `pnpm web:dev` is only a
  renderer preview.
- Runtime-specific code must fail closed: if detection is uncertain, the UI
  should show the option as unavailable or experimental.

## Open Decisions

- ASR service transport: HTTP on localhost, Unix/Windows named pipe, stdio, or
  a mixed approach.
- Runtime package format and manifest signing.
- Model source and checksum authority.
- Default macOS text commit method.
- Default Windows text commit method.
- Linux portal-first strategy versus evdev-first strategy.
- Whether benchmark results stay purely local or can be exported manually by
  users for support.
