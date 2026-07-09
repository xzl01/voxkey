<!--
SPDX-FileCopyrightText: 2026 HarryLoong
SPDX-License-Identifier: MIT
-->

# 导入 Qwen3-ASR 模型

本文说明如何把通义千问 Qwen3-ASR 模型接入本仓库的语音输入系统。仓库只保存代码、配置模板和部署文档，不提交模型权重、ONNX 文件、GGUF 文件或下载压缩包。

推荐目录布局：

```text
$HOME/AI/
├── VoiceInput/                  # 本仓库
└── Model/
    ├── Qwen3-ASR-GGUF/          # HaujetZhao/Qwen3-ASR-GGUF 项目
    │   ├── transcribe.py
    │   ├── qwen_asr_gguf/
    │   │   └── inference/bin/   # llama.cpp 动态库放这里
    │   └── model-1.7B/          # 模型目录，config.json 指向这里
    └── llama.cpp-build/         # 本地编译 llama.cpp 的源码/构建目录
```

## 1. 准备 Python 环境

```bash
python3 -m venv "$HOME/qwen3-asr-venv"
"$HOME/qwen3-asr-venv/bin/pip" install --upgrade pip
"$HOME/qwen3-asr-venv/bin/pip" install -r "$HOME/AI/VoxKey/back-end/asr-service/requirements.txt"
```

如果你的仓库路径不是 `$HOME/AI/VoxKey`，把命令里的路径替换为实际位置。

## 2. 拉取 Qwen3-ASR-GGUF 项目

```bash
mkdir -p "$HOME/AI/Model"
git clone https://github.com/HaujetZhao/Qwen3-ASR-GGUF.git "$HOME/AI/Model/Qwen3-ASR-GGUF"
```

本语音输入守护进程直接 import 这个项目里的 `qwen_asr_gguf` Python 包，因此 `asr_project_dir` 必须指向这个目录。

## 3. 下载预转换 GGUF 模型

推荐优先使用 1.7B 模型，识别质量明显更好。0.6B 模型体积更小，适合快速验证。

### 1.7B 模型

```bash
cd "$HOME/AI/Model/Qwen3-ASR-GGUF"
curl -L -o Qwen3-ASR-1.7B-gguf.zip \
  "https://github.com/HaujetZhao/Qwen3-ASR-GGUF/releases/download/models/Qwen3-ASR-1.7B-gguf.zip"
mkdir -p model-1.7B
unzip -o Qwen3-ASR-1.7B-gguf.zip -d model-1.7B
```

### 0.6B 模型

```bash
cd "$HOME/AI/Model/Qwen3-ASR-GGUF"
curl -L -o Qwen3-ASR-0.6B-gguf.zip \
  "https://github.com/HaujetZhao/Qwen3-ASR-GGUF/releases/download/models/Qwen3-ASR-0.6B-gguf.zip"
mkdir -p model-0.6B
unzip -o Qwen3-ASR-0.6B-gguf.zip -d model-0.6B
```

## 4. 检查模型目录

模型目录中通常应包含：

```text
qwen3_asr_encoder_frontend.int4.onnx
qwen3_asr_encoder_backend.int4.onnx
qwen3_asr_llm.q4_k.gguf
```

当前守护进程使用 int4 encoder，并通过 llama.cpp/Vulkan 加载 GGUF decoder。若 upstream 默认代码或配置查找 `qwen3_asr_llm.q5_k.gguf`，而下载包只有 `q4_k` 文件，可建立兼容 symlink：

```bash
cd "$HOME/AI/Model/Qwen3-ASR-GGUF/model-1.7B"
ln -sf qwen3_asr_llm.q4_k.gguf qwen3_asr_llm.q5_k.gguf
```

0.6B 模型同理：

```bash
cd "$HOME/AI/Model/Qwen3-ASR-GGUF/model-0.6B"
ln -sf qwen3_asr_llm.q4_k.gguf qwen3_asr_llm.q5_k.gguf
```

## 5. 本地编译 llama.cpp Vulkan 后端

Arch Linux 上，Ubuntu 预编译动态库可能 coredump。推荐本地编译 Vulkan 版本：

```bash
git clone --depth 1 --branch b9106 https://github.com/ggml-org/llama.cpp.git "$HOME/AI/Model/llama.cpp-build"
cmake -S "$HOME/AI/Model/llama.cpp-build" \
  -B "$HOME/AI/Model/llama.cpp-build/build" \
  -DGGML_VULKAN=ON \
  -DCMAKE_BUILD_TYPE=Release \
  -G Ninja
cmake --build "$HOME/AI/Model/llama.cpp-build/build" --parallel "$(nproc)"
```

把运行所需的共享库复制进 Qwen3-ASR-GGUF 项目：

```bash
mkdir -p "$HOME/AI/Model/Qwen3-ASR-GGUF/qwen_asr_gguf/inference/bin"
cp -a "$HOME"/AI/Model/llama.cpp-build/build/bin/libggml*.so* \
      "$HOME"/AI/Model/llama.cpp-build/build/bin/libllama*.so* \
      "$HOME/AI/Model/Qwen3-ASR-GGUF/qwen_asr_gguf/inference/bin/"
```

Intel iGPU 建议禁用 Vulkan FP16，避免输出乱码或连续感叹号：

```bash
export GGML_VK_DISABLE_F16=1
```

本仓库的 `run.sh` 默认已经设置：

```text
GGML_VK_DISABLE_F16=1
LD_LIBRARY_PATH=$QWEN_ASR_PROJECT_DIR/qwen_asr_gguf/inference/bin:$LD_LIBRARY_PATH
```

## 6. 配置 VoiceInput 指向模型

复制示例配置：

```bash
cd "$HOME/AI/VoxKey"
cp config.example.json config.json
```

1.7B 推荐配置：

```json
{
  "asr_project_dir": "$HOME/AI/Model/Qwen3-ASR-GGUF",
  "model_dir": "$HOME/AI/Model/Qwen3-ASR-GGUF/model-1.7B",
  "python_venv": "$HOME/qwen3-asr-venv",
  "language": "Chinese"
}
```

0.6B 验证配置：

```json
{
  "asr_project_dir": "$HOME/AI/Model/Qwen3-ASR-GGUF",
  "model_dir": "$HOME/AI/Model/Qwen3-ASR-GGUF/model-0.6B",
  "python_venv": "$HOME/qwen3-asr-venv",
  "language": "Chinese"
}
```

`config.json` 是本机文件，已被 `.gitignore` 排除，不会提交到 GitHub。

## 7. 单独验证模型转写

先录一段测试音频：

```bash
pw-record --rate 16000 --channels 1 --format s16 /tmp/qwen-test.wav
```

按 `Ctrl+C` 停止后，用 VoiceInput 入口转写：

```bash
cd "$HOME/AI/VoxKey"
./run.sh --transcribe-file /tmp/qwen-test.wav
```

如果这里能输出文字，说明 Python 环境、Qwen3-ASR-GGUF 项目、模型目录和 llama.cpp 共享库都已经导入成功。

## 8. 常见问题

`No module named qwen_asr_gguf`：`asr_project_dir` 没有指向 `Qwen3-ASR-GGUF` 项目根目录。

`libllama.so` 或 `libggml*.so` 找不到：确认共享库已经复制到 `qwen_asr_gguf/inference/bin/`，或设置了正确的 `LD_LIBRARY_PATH`。

输出是 `!!!!` 或乱码：在 Intel iGPU/Vulkan 上确认 `GGML_VK_DISABLE_F16=1` 生效。

模型文件找不到：检查 `model_dir`，并确认 `qwen3_asr_encoder_frontend.int4.onnx`、`qwen3_asr_encoder_backend.int4.onnx`、`qwen3_asr_llm.q4_k.gguf` 存在。

GitHub 仓库太大：不要把模型目录放进本仓库；若临时放入，`.gitignore` 已排除常见模型文件和 `models/` 目录。
