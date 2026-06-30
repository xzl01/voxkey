<!--
SPDX-FileCopyrightText: 2026 HarryLoong
SPDX-License-Identifier: MIT
-->

# VoxKey 语音输入工具：Arch Linux + niri

> 说明：本文是从本机部署记录导入的历史文档，包含当时的 `/home/xzl/...` 路径和服务状态。仓库化后的通用安装方式以根目录 `README.md` 和 `INSTALL.md` 为准。

本文记录当前已经跑通的本地语音输入方案：在 Arch Linux / niri Wayland 环境下，用 Lenovo 键盘特殊按键触发录音，松开后调用 Qwen3-ASR-GGUF 转写，并用 `wtype` 输入到当前聚焦窗口。

## 当前结论

- 使用 Qwen3-ASR-GGUF 1.7B + Vulkan/iGPU。
- 最终语音输入触发键：`/dev/input/event11` 上的 `code=193`。
- 触发模式：`hold`。
  - 按住：开始录音。
  - 松开：停止录音、转写、上屏。
- 屏幕提示：启用 `notify-send`。
- 上屏方式：`wtype`。
- 剪贴板复制：当前关闭。
  - 原因：`wl-copy` 曾超时，导致转写成功后没能继续执行 `wtype`。
  - 当前优先保证语音输入主链路可用。

## 文件位置

### 语音输入工具

```text
/home/xzl/AI/VoxKey/
├── config.json
├── voice_input_daemon.py
├── run.sh
├── README.md
└── recordings/
```

### systemd user service

```text
/home/xzl/.config/systemd/user/voxkey.service
```

当前 service 内容：

```ini
[Unit]
Description=VoxKey local voice input
After=graphical-session.target pipewire.service

[Service]
Type=simple
ExecStart=/home/xzl/AI/VoxKey/run.sh
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

### 按键监听辅助脚本

```text
/home/xzl/key-listener.py
```

用于直接监听 `/dev/input/event*`，确认特殊键实际设备和 key code。

### Qwen3-ASR-GGUF 路径

```text
项目目录：/home/xzl/AI/Model/Qwen3-ASR-GGUF
模型目录：/home/xzl/AI/Model/Qwen3-ASR-GGUF/model-1.7B
venv：/home/xzl/qwen3-asr-venv
llama/Vulkan .so：/home/xzl/AI/Model/Qwen3-ASR-GGUF/qwen_asr_gguf/inference/bin
```

## 当前配置

`/home/xzl/AI/VoxKey/config.json` 当前关键配置：

```json
{
  "input_device": "/dev/input/event11",
  "trigger_code": 193,
  "trigger_name": "Lenovo voice input key / code 193",
  "trigger_mode": "hold",
  "recordings_dir": "/home/xzl/AI/VoxKey/recordings",
  "asr_project_dir": "/home/xzl/AI/Model/Qwen3-ASR-GGUF",
  "model_dir": "/home/xzl/AI/Model/Qwen3-ASR-GGUF/model-1.7B",
  "python_venv": "/home/xzl/qwen3-asr-venv",
  "language": "Chinese",
  "min_record_seconds": 0.25,
  "pw_record": {
    "rate": 16000,
    "channels": 1,
    "format": "s16"
  },
  "type_command": "wtype",
  "copy_to_clipboard": false,
  "type_text": true,
  "notify": true,
  "notify_timeout_ms": 1200,
  "strip_trailing_punctuation": false
}
```

## 运行方式

> 当前已合并到统一用户级监听服务：`custom-key-daemon.service`。如果只想临时单独测试语音输入，仍可运行 `/home/xzl/AI/VoxKey/run.sh`。

统一监听器位置：

```text
/home/xzl/AI/CustomKeyDaemon/
├── config.json
├── custom_key_daemon.py
└── run.sh
```

统一服务：

```text
/home/xzl/.config/systemd/user/custom-key-daemon.service
```

当前统一监听内容：

```text
听写：/dev/input/event11 code=193 hold
护眼：/dev/input/event6  code=202 press -> /home/xzl/.local/bin/toggle-eye-care
```

旧的用户级护眼监听服务 `eye-key-listener.service` 已停用，避免重复触发。旧的单独听写服务 `voxkey.service` 当前也是 inactive/disabled。

手动运行：

```bash
/home/xzl/AI/VoxKey/run.sh
```

启动后应看到类似：

```text
Loading Qwen3-ASR engine: /home/xzl/AI/Model/Qwen3-ASR-GGUF/model-1.7B
Qwen3-ASR engine ready
Listening for Lenovo voice input key / code 193 code=193 mode=hold on /dev/input/event11
```

使用方式：

1. 在可输入文字的窗口聚焦光标。
2. 按住 `code=193` 对应的 Lenovo 特殊键。
3. 屏幕应弹出 `正在录音…`。
4. 说话。
5. 松开按键。
6. 屏幕应提示录音结束、正在转写、语音输入完成。
7. 文字通过 `wtype` 输入到当前窗口。

## 自检命令

```bash
/home/xzl/AI/VoxKey/run.sh --self-test
```

期望结果包括：

```text
✅ input device exists: /dev/input/event11
✅ input device readable: /dev/input/event11
✅ ASR project exists: /home/xzl/AI/Model/Qwen3-ASR-GGUF
✅ model dir exists: /home/xzl/AI/Model/Qwen3-ASR-GGUF/model-1.7B
✅ venv python exists: /home/xzl/qwen3-asr-venv
✅ pw-record available
✅ wtype available
✅ wl-copy available
✅ notify-send available
```

## 单独测试通知

```bash
notify-send --app-name "简听输入" --expire-time 3000 "测试通知" "如果看到这个，通知没问题"
```

如果看不到通知，问题在通知系统或通知 daemon，不一定是语音输入脚本。

## 单独测试录音与转写

录音：

```bash
pw-record --rate 16000 --channels 1 --format s16 /tmp/qwen-test.wav
```

说一句话后按 `Ctrl+C` 停止。

确认文件：

```bash
ls -lh /tmp/qwen-test.wav
```

转写：

```bash
/home/xzl/AI/VoxKey/run.sh --transcribe-file /tmp/qwen-test.wav
```

如果这里能输出文字，说明模型、录音、转写链路正常。

## 单独测试按键监听

监听当前最终设备：

```bash
/home/xzl/key-listener.py /dev/input/event11
```

按住并松开目标键，应看到类似：

```text
/dev/input/event11 code=193 KEY_193 / Lenovo voice input candidate DOWN
/dev/input/event11 code=193 KEY_193 / Lenovo voice input candidate REPEAT
/dev/input/event11 code=193 KEY_193 / Lenovo voice input candidate UP
```

如果这里看不到 `code=193`，说明设备号或按键发生变化，需要重新找实际设备和 code。

也可以不指定设备，监听所有当前可读 event 设备：

```bash
/home/xzl/key-listener.py
```

## 语音脚本调试日志

运行：

```bash
/home/xzl/AI/VoxKey/run.sh
```

按住目标键时，应看到：

```text
Trigger event: DOWN (1)
Recording started: /home/xzl/AI/VoxKey/recordings/voice-....wav
Trigger event: REPEAT (2)
```

松开时，应看到：

```text
Trigger event: UP (0)
Recording stopped (...s): /home/xzl/AI/VoxKey/recordings/voice-....wav
Transcribed: ...
Typed text: ...
```

如果出现：

```text
Recording too short
```

说明按得太短。当前 `min_record_seconds` 是 `0.25`，建议实际说话时按住 1 秒以上。

## 启用 systemd user 服务

当前推荐启用统一服务：

```bash
systemctl --user daemon-reload
systemctl --user enable --now custom-key-daemon.service
systemctl --user status custom-key-daemon.service --no-pager
```

查看统一服务日志：

```bash
journalctl --user -u custom-key-daemon.service -f
```

统一服务自己的日志文件：

```text
/home/xzl/.cache/custom-key-daemon/daemon.log
```

旧的单独语音输入服务保留但不推荐启用，除非要回退测试：

```bash
systemctl --user daemon-reload
systemctl --user enable --now voxkey.service
systemctl --user status voxkey.service --no-pager
```

查看日志：

```bash
journalctl --user -u voxkey.service -f
```

停止服务：

```bash
systemctl --user stop voxkey.service
```

禁用自启动：

```bash
systemctl --user disable voxkey.service
```

## 已踩坑记录

### 1. `/dev/input/event6` + `code=148` 不适合 hold 模式

早期尝试过：

```json
"input_device": "/dev/input/event6",
"trigger_code": 148
```

但该键看起来会快速发送 `DOWN` / `UP`，不适合“按住说话”。后来为它加过 `toggle` 模式，但最终还是改用更适合 hold 的 `/dev/input/event11 code=193`。

### 2. `/dev/input/event11` + `code=193` 支持长按

该键会产生 `REPEAT`，适合按住说话：

```text
code=193 REPEAT
code=193 REPEAT
code=193 REPEAT
```

最终采用：

```json
"input_device": "/dev/input/event11",
"trigger_code": 193,
"trigger_mode": "hold"
```

### 3. `wl-copy` 曾阻塞主链路

曾出现：

```text
Transcription failed: TimeoutExpired(['wl-copy'], 5)
```

这说明转写本身已经成功，但复制到剪贴板超时，导致后面的 `wtype` 没执行。

当前处理：

- `copy_to_clipboard` 设为 `false`。
- 即使以后重新打开剪贴板复制，脚本也会捕获 `wl-copy` 超时，不再中断上屏。

### 4. `pw-record exited with 1` 不一定代表失败

停止 `pw-record` 时脚本会发信号结束录音，日志中可能看到：

```text
pw-record exited with 1: /path/to/voice.wav
```

只要后续出现：

```text
Recording stopped (...s): /path/to/voice.wav
```

且文件存在，就通常不影响转写。

### 5. 通知只是提示，不是核心链路

屏幕提示通过 `notify-send` 实现：

- `正在录音…`
- `录音结束`
- `正在转写…`
- `语音输入完成`
- `语音输入失败`

如果通知不可见，但终端日志显示录音和转写正常，则语音输入主链路仍可能是正常的。

## 关键环境变量

`/home/xzl/AI/VoxKey/run.sh` 设置：

```bash
export GGML_VK_DISABLE_F16=1
export LD_LIBRARY_PATH="/home/xzl/AI/Model/Qwen3-ASR-GGUF/qwen_asr_gguf/inference/bin:${LD_LIBRARY_PATH:-}"
exec /home/xzl/qwen3-asr-venv/bin/python3 /home/xzl/AI/VoxKey/voice_input_daemon.py "$@"
```

`GGML_VK_DISABLE_F16=1` 保留，用来规避 Vulkan F16 相关问题。

## 相关文档

Qwen3-ASR-GGUF 在 Arch Linux 上的部署、模型、Vulkan 编译和踩坑记录见：

```text
/home/xzl/AI/qwen3-asr-gguf-arch-linux.md
```
