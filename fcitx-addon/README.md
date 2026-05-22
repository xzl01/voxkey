<!--
SPDX-FileCopyrightText: 2026 HarryLoong
SPDX-License-Identifier: MIT
-->

# fcitx5 Qwen Voice Input Addon

This addon is a small local IPC bridge for Qwen Voice Input. Python records
audio and runs ASR; this C++ addon owns the fcitx5 commit path.

It listens on:

```text
$XDG_RUNTIME_DIR/qwen-voice-input-fcitx.sock
```

Protocol:

```text
empty datagram       -> PONG
COMMIT\n<utf8 text> -> OK or ERR ...
<utf8 text>          -> OK or ERR ...  (compatibility mode)
```

## Build

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build
```

## User Install

```bash
./install-user.sh
fcitx5 -rd
```

The install script writes a user addon config under
`$HOME/.local/share/fcitx5/addon/` with an absolute library path. This avoids
depending on whether the running fcitx5 build searches `$HOME/.local/lib/fcitx5`.

## System Install

```bash
cmake --install build --prefix /usr
fcitx5 -rd
```
