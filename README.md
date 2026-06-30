<!--
SPDX-FileCopyrightText: 2026 HarryLoong
SPDX-License-Identifier: MIT
-->

# 简听输入 (VoxKey)

![VoxKey architecture](docs/assets/voxkey-architecture.svg)

简听输入 (VoxKey) 是一个本地优先的语音输入工具。当前 Linux / Wayland 原型支持用户显式绑定触发键：按住按键开始录音，松开后调用本地 Qwen3-ASR-GGUF 转写，并把文本提交到当前聚焦输入框。

这个仓库从一套已经跑通的 Arch Linux + niri + Intel iGPU + fcitx5 部署整理而来，但默认配置不绑定任何硬件按键，也不会主动监听 `/dev/input/event*`。用户必须先检测并确认自己的触发键，再把 `trigger.enabled` 改为 `true`。

## 核心链路

```text
用户触发键 -> evdev (/dev/input/event*) -> pw-record -> Qwen3-ASR-GGUF
                                                        -> fcitx5 addon -> 当前输入框
                                                        -> wtype fallback
```

## 功能

- 默认不绑定、不监听任何按键，避免把作者本机 Lenovo 特殊键带到其他用户环境。
- 支持 `hold` 模式：按住录音，松开转写并上屏。
- 支持 `toggle` 模式：按一次开始录音，再按一次结束。
- 支持按设备名动态解析 `/dev/input/event*`，降低重启后 event 编号漂移的影响。
- 录音使用 PipeWire `pw-record`。
- 转写使用本地 Qwen3-ASR-GGUF，模型和权重不进入本仓库。
- 优先通过 fcitx5 addon 提交文本，失败时回退到 `wtype`。
- 可选复制到剪贴板，可选桌面通知。
- 提供标准库 `unittest` 单元测试，不依赖 pytest。

## 仓库内容

```text
.
├── voice_input_daemon.py
├── run.sh
├── config.example.json
├── requirements-asr.txt
├── LICENSE
├── apps/desktop-ui/
├── crates/voxkey-core/
├── services/asr-service/
├── fcitx-addon/
│   ├── CMakeLists.txt
│   ├── voxkeyinput.cpp
│   ├── voxkeyinput.conf
│   ├── install-user.sh
│   └── README.md
├── systemd/user/voxkey.service
├── tests/test_voice_input_daemon.py
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT.md
│   ├── ROADMAP.md
│   ├── assets/voxkey-architecture.svg
│   ├── import-qwen3-asr-model.md
│   ├── voxkey-arch-niri.md
│   └── qwen3-asr-gguf-arch-linux.md
└── INSTALL.md
```

本仓库不包含：

- Qwen3-ASR 模型文件
- GGUF / ONNX 权重
- llama.cpp 构建产物
- Python venv
- 录音缓存
- 本机 `config.json`

这些文件已经在 `.gitignore` 中排除。

## 当前已验证环境

- Arch Linux x86_64
- niri / Wayland
- PipeWire
- fcitx5
- Intel Lunar Lake + Arc Graphics 130V/140V
- Qwen3-ASR-GGUF 1.7B
- llama.cpp Vulkan 后端

其他发行版和桌面环境需要按实际包名、输入法和权限模型调整。

## 跨平台重构计划

简听输入 (VoxKey) 正在从 Linux / Wayland 原型迁移为跨平台桌面应用。协作时优先查看：

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：跨平台架构、模块边界和运行时模型。
- [docs/ROADMAP.md](docs/ROADMAP.md)：阶段计划、模块 backlog、协作规则和待决策问题。
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)：桌面应用、本地服务和验证命令。

## 快速开始

安装细节见 [INSTALL.md](INSTALL.md)，模型导入细节见 [docs/import-qwen3-asr-model.md](docs/import-qwen3-asr-model.md)。

```bash
cp config.example.json config.json
./run.sh --list-devices
./run.sh --detect-key
./run.sh --self-test
./run.sh
```

默认示例配置中的触发键是关闭的：

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

`./run.sh --detect-key` 会输出检测到的设备名、event fallback、key code 和建议模式。确认无误后，把结果写入 `config.json` 的 `trigger` 块，并把 `enabled` 改为 `true`。

示例：

```json
{
  "trigger": {
    "enabled": true,
    "backend": "evdev",
    "input_name": "keyd virtual keyboard",
    "input_device": "/dev/input/event13",
    "code": 193,
    "name": "voice input key",
    "mode": "hold"
  }
}
```

建议优先保存 `input_name`，把 `input_device` 只当 fallback。这样重启后 `/dev/input/event*` 编号变化时，程序仍可按设备名解析当前 event 设备。

## fcitx5 Addon

fcitx5 addon 是推荐的上屏路径。它监听本机 Unix datagram socket，接收 Python daemon 发来的文本，并通过 fcitx5 当前输入上下文提交。Python daemon 仍保留 `wtype` fallback。

```bash
cd fcitx-addon
./install-user.sh
fcitx5 -rd
cd ..
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

## 模型导入

模型不进入本仓库。推荐目录：

```text
$HOME/AI/
├── VoiceInput/
└── Model/
    ├── Qwen3-ASR-GGUF/
    └── llama.cpp-build/
```

最小流程：

```bash
python3 -m venv "$HOME/qwen3-asr-venv"
"$HOME/qwen3-asr-venv/bin/pip" install -r requirements-asr.txt
git clone https://github.com/HaujetZhao/Qwen3-ASR-GGUF.git "$HOME/AI/Model/Qwen3-ASR-GGUF"
```

然后下载 1.7B 或 0.6B 预转换模型，并按文档复制 llama.cpp Vulkan 动态库。完整步骤见 [导入 Qwen3-ASR 模型](docs/import-qwen3-asr-model.md)。

## 运行时环境变量

```text
QWEN_VOICE_INPUT_CONFIG  配置文件路径，默认 ./config.json
QWEN_ASR_PROJECT_DIR     Qwen3-ASR-GGUF 项目路径，默认 $HOME/AI/Model/Qwen3-ASR-GGUF
QWEN_ASR_VENV            Python venv，默认 $HOME/qwen3-asr-venv
QWEN_ASR_PYTHON          直接指定 Python 解释器，优先级最高
GGML_VK_DISABLE_F16      默认 1
```

## 测试

```bash
python -m unittest discover -s tests -v
python -m py_compile voice_input_daemon.py tests/test_voice_input_daemon.py
python -m json.tool config.example.json >/dev/null
cmake -S fcitx-addon -B fcitx-addon/build -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build fcitx-addon/build
```

当前单元测试覆盖：

- 新旧配置格式解析
- 默认不监听按键的安全行为
- 输入设备解析和按键检测
- self-test 在 trigger 关闭时跳过 input device 权限检查
- fcitx5 提交成功路径
- fcitx5 失败后 `wtype` fallback
- `wl-copy` 超时不阻断上屏

## 安全与权限

直接读取 `/dev/input/event*` 有安全含义，因为具备读取键盘事件的能力。不要默认把用户加入 `input` 组。更稳妥的方式是只授权目标设备，或使用桌面会话/logind 当前 seat ACL。

本项目默认不启用按键监听，只有在用户显式配置 `trigger.enabled=true` 后才会读取 input event。

## 开源许可证

本仓库自有代码、文档、测试和 SVG 配图采用 MIT License，见 [LICENSE](LICENSE)。

外部项目和运行时依赖不随本仓库再分发，并遵循各自上游许可证：

- Qwen3-ASR-GGUF 项目和 Qwen 模型权重由用户自行下载并遵循上游许可。
- llama.cpp 动态库由用户自行编译或安装并遵循上游许可。
- fcitx5 运行时和开发库在 Arch Linux 包中标注为 `LGPL-2.1-or-later AND Unicode-DFS-2016`；本仓库的 fcitx5 addon 源码采用 MIT，并动态链接本机 fcitx5。
- Python、PipeWire、wtype、wl-clipboard、libnotify 等运行时依赖遵循各自上游许可。

仓库内文件使用 SPDX 标注。JSON、许可证文本等不适合内嵌 SPDX 注释的文件，通过 `.reuse/dep5` 声明版权和许可证。

## 文档

- [安装说明](INSTALL.md)
- [导入 Qwen3-ASR 模型](docs/import-qwen3-asr-model.md)
- [Arch Linux + niri 语音输入部署记录](docs/voxkey-arch-niri.md)
- [Qwen3-ASR-GGUF 在 Arch Linux + Intel GPU 上部署记录](docs/qwen3-asr-gguf-arch-linux.md)
