<!--
SPDX-FileCopyrightText: 2026 HarryLoong
SPDX-License-Identifier: MIT
-->

# 安装说明

本文档面向 Arch Linux / niri / Wayland 环境。其他发行版可按同类包名替换。

## 1. 系统依赖

```bash
sudo pacman -S --needed python ffmpeg pipewire wireplumber wtype wl-clipboard libnotify cmake ninja pkgconf fcitx5 vulkan-intel mesa
```

如果要编译 upstream `llama.cpp`，还需要常规 C/C++ 构建工具：

```bash
sudo pacman -S --needed base-devel git
```

## 2. 部署 Qwen3-ASR-GGUF

详细模型导入流程见 [docs/import-qwen3-asr-model.md](docs/import-qwen3-asr-model.md)。最小步骤如下：

```bash
python3 -m venv "$HOME/qwen3-asr-venv"
"$HOME/qwen3-asr-venv/bin/pip" install -r requirements-asr.txt

git clone https://github.com/HaujetZhao/Qwen3-ASR-GGUF.git "$HOME/AI/Model/Qwen3-ASR-GGUF"
```

在 Arch Linux 上，GitHub Release 的 Ubuntu 预编译 llama.cpp 共享库可能崩溃。建议按文档本地编译 Vulkan 版 `llama.cpp`，并把 `libllama.so`、`libggml*.so` 复制到：

```text
$HOME/AI/Model/Qwen3-ASR-GGUF/qwen_asr_gguf/inference/bin/
```

下载 1.7B 预转换模型：

```bash
cd "$HOME/AI/Model/Qwen3-ASR-GGUF"
curl -L -o Qwen3-ASR-1.7B-gguf.zip \
  "https://github.com/HaujetZhao/Qwen3-ASR-GGUF/releases/download/models/Qwen3-ASR-1.7B-gguf.zip"
mkdir -p model-1.7B
unzip -o Qwen3-ASR-1.7B-gguf.zip -d model-1.7B
cd model-1.7B
ln -sf qwen3_asr_llm.q4_k.gguf qwen3_asr_llm.q5_k.gguf
```

Intel iGPU 推荐保留：

```bash
export GGML_VK_DISABLE_F16=1
```

`run.sh` 默认会设置这个变量。

## 3. 配置语音输入

```bash
cp config.example.json config.json
```

示例配置默认不绑定任何按键：

```json
{
  "trigger": {
    "enabled": false,
    "backend": "evdev",
    "input_name": null,
    "input_device": null,
    "code": null,
    "mode": "hold"
  }
}
```

列出本机输入设备：

```bash
./run.sh --list-devices
```

检测你想用作语音输入键的按键：

```bash
./run.sh --detect-key
```

如果只想监听某个设备，传入设备路径：

```bash
./run.sh --detect-key /dev/input/event11
```

程序会输出类似：

```text
Detected:
  input_name: "keyd virtual keyboard"
  input_device: "/dev/input/event11"
  code: 193
  suggested_mode: hold
```

把结果写入 `config.json`：

```json
{
  "trigger": {
    "enabled": true,
    "backend": "evdev",
    "input_name": "keyd virtual keyboard",
    "input_device": "/dev/input/event11",
    "code": 193,
    "name": "voice input key",
    "mode": "hold"
  }
}
```

建议优先保存 `input_name`，`input_device` 只作为 fallback，这样重启后 `/dev/input/event*` 编号漂移时仍有机会自动解析。

权限提醒：读取 `/dev/input/event*` 需要相应权限。把用户加入 `input` 组虽然简单，但权限较大，理论上允许程序读取键盘事件。更稳妥的做法是只授权目标设备，或使用桌面会话/logind 提供的当前 seat ACL。

## 4. 安装 fcitx5 addon

fcitx5 addon 是推荐的上屏路径；失败时守护进程会回退到 `wtype`。

```bash
cd fcitx-addon
./install-user.sh
fcitx5 -rd
cd ..
```

验证：

```bash
./run.sh --ping-fcitx
```

期望输出：

```text
PONG
```

如暂时不使用 fcitx5 addon，可在 `config.json` 中设置：

```json
"fcitx_commit": false
```

## 5. 自检与手动运行

```bash
./run.sh --self-test
./run.sh
```

转写已有音频：

```bash
./run.sh --transcribe-file /tmp/qwen-test.wav
```

转写并上屏：

```bash
./run.sh --transcribe-file /tmp/qwen-test.wav --type
```

## 6. systemd user 服务

本仓库提供的 service 默认安装路径是 `%h/AI/VoiceInput`。如果仓库放在其他位置，先编辑 `systemd/user/qwen-voice-input.service`。

```bash
mkdir -p "$HOME/.config/systemd/user"
cp systemd/user/qwen-voice-input.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now qwen-voice-input.service
```

日志：

```bash
journalctl --user -u qwen-voice-input.service -f
```
