<!--
SPDX-FileCopyrightText: 2026 HarryLoong
SPDX-License-Identifier: MIT
-->

# Qwen3-ASR-GGUF 在 Arch Linux + Intel GPU 上部署记录

> 说明：本文是从本机部署记录导入的历史文档，包含当时的 `/home/xzl/...` 路径。仓库化后的通用安装方式以根目录 `README.md` 和 `INSTALL.md` 为准。

> 日期: 2026-05-12
> 设备: Intel Lunar Lake (Arc Graphics 130V/140V), Arch Linux x86_64, 30GB RAM
> 日期: 2026-05-12

## 项目地址

https://github.com/HaujetZhao/Qwen3-ASR-GGUF

## 环境

| 项目 | 版本/型号 |
|------|-----------|
| OS | Arch Linux (rolling) |
| CPU | Intel Lunar Lake |
| GPU | Intel Arc Graphics 130V/140V (Xe2) |
| RAM | 30 GB |
| Python | 3.14.4 |
| Vulkan 驱动 | vulkan-intel 1:26.0.6-1, Mesa 1:26.0.6-1 |
| ffmpeg | 系统自带 |

## 安装步骤

### 1. 创建虚拟环境

```bash
python3 -m venv /home/xzl/qwen3-asr-venv
```

### 2. 安装 Python 依赖

```bash
/home/xzl/qwen3-asr-venv/bin/pip install onnxruntime typer rich pydub numpy scipy gguf srt librosa soundfile
```

### 3. 克隆仓库

```bash
git clone https://github.com/HaujetZhao/Qwen3-ASR-GGUF.git /home/xzl/AI/Model/Qwen3-ASR-GGUF
```

### 4. 编译 llama.cpp (Vulkan)

**关键坑**: GitHub Releases 的 Ubuntu 预编译二进制在 Arch 上会 coredump，必须本地编译。

```bash
git clone --depth 1 --branch b9106 https://github.com/ggml-org/llama.cpp.git /home/xzl/AI/Model/llama.cpp-build
cd /home/xzl/AI/Model/llama.cpp-build
mkdir -p build && cd build
cmake .. -DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release -G Ninja
ninja -j$(nproc)
```

### 5. 复制 .so 到项目 bin 目录

```bash
mkdir -p /home/xzl/AI/Model/Qwen3-ASR-GGUF/qwen_asr_gguf/inference/bin

cp -a /home/xzl/AI/Model/llama.cpp-build/build/bin/libggml.so \
       /home/xzl/AI/Model/llama.cpp-build/build/bin/libggml.so.0 \
       /home/xzl/AI/Model/llama.cpp-build/build/bin/libggml.so.0.11.1 \
       /home/xzl/AI/Model/llama.cpp-build/build/bin/libggml-base.so \
       /home/xzl/AI/Model/llama.cpp-build/build/bin/libggml-base.so.0 \
       /home/xzl/AI/Model/llama.cpp-build/build/bin/libggml-base.so.0.11.1 \
       /home/xzl/AI/Model/llama.cpp-build/build/bin/libllama.so \
       /home/xzl/AI/Model/llama.cpp-build/build/bin/libllama.so.0 \
       /home/xzl/AI/Model/llama.cpp-build/build/bin/libllama.so.0.0.1 \
       /home/xzl/AI/Model/llama.cpp-build/build/bin/libggml-vulkan.so \
       /home/xzl/AI/Model/llama.cpp-build/build/bin/libggml-vulkan.so.0 \
       /home/xzl/AI/Model/llama.cpp-build/build/bin/libggml-vulkan.so.0.11.1 \
       /home/xzl/AI/Model/Qwen3-ASR-GGUF/qwen_asr_gguf/inference/bin/
```

### 6. 下载预转换模型

```bash
# 0.6B 模型 (~538MB, 快)
curl -L -o /home/xzl/AI/Model/Qwen3-ASR-GGUF/Qwen3-ASR-0.6B-gguf.zip \
  "https://github.com/HaujetZhao/Qwen3-ASR-GGUF/releases/download/models/Qwen3-ASR-0.6B-gguf.zip"
unzip -o /home/xzl/AI/Model/Qwen3-ASR-GGUF/Qwen3-ASR-0.6B-gguf.zip -d /home/xzl/AI/Model/Qwen3-ASR-GGUF/model/

# 1.7B 模型 (~1.3GB, 更准, 实际更快)
curl -L -o /home/xzl/AI/Model/Qwen3-ASR-GGUF/Qwen3-ASR-1.7B-gguf.zip \
  "https://github.com/HaujetZhao/Qwen3-ASR-GGUF/releases/download/models/Qwen3-ASR-1.7B-gguf.zip"
unzip -o /home/xzl/AI/Model/Qwen3-ASR-GGUF/Qwen3-ASR-1.7B-gguf.zip -d /home/xzl/AI/Model/Qwen3-ASR-GGUF/model-1.7B/

# 创建 symlink (默认配置期望 q5_k, 实际是 q4_k)
cd /home/xzl/AI/Model/Qwen3-ASR-GGUF/model && ln -sf qwen3_asr_llm.q4_k.gguf qwen3_asr_llm.q5_k.gguf
cd /home/xzl/AI/Model/Qwen3-ASR-GGUF/model-1.7B && ln -sf qwen3_asr_llm.q4_k.gguf qwen3_asr_llm.q5_k.gguf
```

## 使用方式

### 环境变量

```bash
export GGML_VK_DISABLE_F16=1
export LD_LIBRARY_PATH="/home/xzl/AI/Model/Qwen3-ASR-GGUF/qwen_asr_gguf/inference/bin:$LD_LIBRARY_PATH"
```

> `GGML_VK_DISABLE_F16=1` 是必须的，Intel 集显 Vulkan FP16 计算可能溢出，导致输出乱码或 "!!!!"

### 运行转录

```bash
# 使用 0.6B 模型
/home/xzl/qwen3-asr-venv/bin/python3 /home/xzl/AI/Model/Qwen3-ASR-GGUF/transcribe.py \
  你的音频.mp3 \
  --model-dir /home/xzl/AI/Model/Qwen3-ASR-GGUF/model \
  --prec int4 \
  --provider CPU \
  --verbose -y

# 使用 1.7B 模型
/home/xzl/qwen3-asr-venv/bin/python3 /home/xzl/AI/Model/Qwen3-ASR-GGUF/transcribe.py \
  你的音频.mp3 \
  --model-dir /home/xzl/AI/Model/Qwen3-ASR-GGUF/model-1.7B \
  --prec int4 \
  --provider CPU \
  --verbose -y
```

### 常用参数

| 参数 | 说明 |
|------|------|
| `--model-dir` | 模型目录 |
| `--prec int4` | 编码器精度 (fp32/fp16/int8/int4) |
| `--provider CPU` | ONNX 后端 (Linux 只能用 CPU) |
| `--no-ts` | 关闭时间戳对齐（加快速度） |
| `--no-vulkan` | 关闭 Vulkan，纯 CPU |
| `--language Chinese` | 强制指定语种 |
| `-y` | 覆盖已存在的输出文件 |

## 实测性能

### 0.6B vs 1.7B 对比

| | 0.6B | 1.7B |
|---|---|---|
| 模型大小 | 462 MB | 1.2 GB |
| 引擎初始化 | 0.56s | 0.66s |
| RTF (实时率) | 0.084 | 0.197 |
| LLM 生成速度 | 11.7 t/s | 33.6 t/s |
| 准确率 | 一般 | 高 |

### 1.7B 长句识别测试

```
输入: "今天天气真好，适合出去散步。人工智能正在改变我们的生活方式，语音识别技术已经非常成熟了。"

输出: 今天天气真好，适合出去散步。人工智能正在改变我们的生活方式，语音识别技术已经非常成熟了。

RTF: 0.197 (12.86秒音频, 2.53秒处理)
LLM 预填充: 686 t/s | LLM 生成: 33.6 t/s
```

## 踩过的坑

1. **Ubuntu 预编译 .so 在 Arch 上 coredump** — 必须本地源码编译 llama.cpp
2. **`GGML_VK_DISABLE_F16=1` 必须设置** — Intel 集显 FP16 溢出导致输出 "!!!!"
3. **模型文件名不匹配** — 默认配置期望 `q5_k`，预编译模型是 `q4_k`，需创建 symlink
4. **zsh glob 展开问题** — 批量操作时注意 zsh 对 `*` 的处理，用显式路径代替
5. **Python 3.14.4 兼容** — 部分旧包的 wheel 可能不支持，但 pip 会自动 fallback 到源码编译

## 架构说明

```
音频输入 → ONNX Encoder (CPU) → 特征向量 → GGUF Decoder (Vulkan/GPU) → 文本输出
             ↑ 极轻量 (~0.2s)                    ↑ 绝对主力
```

- **ONNX Encoder**: 跑在 CPU 上，DirectML 是 Windows 独占
- **GGUF Decoder**: 通过 llama.cpp + Vulkan 后端跑在 Intel Arc iGPU 上
- **Vulkan 后端**: `libggml-vulkan.so` — 编译时自动检测 Intel GPU

## NPU 路线探索 (OpenVINO)

### 结论：当前不可行

OpenVINO 官方 notebook 在 Qwen3-ASR 中**明确排除了 NPU 设备**：

```python
device = device_widget("CPU", exclude=["NPU"])
```

参考：https://github.com/openvinotoolkit/openvino_notebooks/tree/latest/notebooks/qwen3-asr

### 实测结果

| 组件 | CPU | GPU | NPU |
|------|-----|-----|-----|
| Audio Conv (CNN) | ✅ | ❌ 缺驱动 | ❌ 算子不支持 |
| Audio Encoder (Transformer) | ✅ | - | ❌ |
| Embedding | ✅ | - | ❌ |
| Language Model (LLM) | ✅ | - | ❌ (FP16/INT8 均失败) |

- NPU 错误: `ZE_RESULT_ERROR_UNSUPPORTED_FEATURE` — NPU 编译器不支持 Qwen3-ASR 的音频特定算子
- OpenVINO NPU 只支持纯文本 Qwen3 LLM（Qwen3-1.7B/4B/8B），不支持 ASR 版的定制化架构
- GPU 未识别：缺少 `intel-compute-runtime` 驱动包

### OpenVINO CPU 模式可用

```bash
# 1. 安装
pip install "openvino>=2025.4.0" qwen-asr torch transformers

# 2. 转换模型
from qwen_3_asr_helper import convert_qwen3_asr_model
convert_qwen3_asr_model(model_id="/path/to/model", output_dir="./ov-model")

# 3. 推理
from qwen_3_asr_helper import OVQwen3ASRModel
ov_model = OVQwen3ASRModel.from_pretrained(model_dir="./ov-model", device="CPU")
results = ov_model.transcribe(audio="audio.mp3")
```

## 相关链接

- Qwen3-ASR-GGUF: https://github.com/HaujetZhao/Qwen3-ASR-GGUF
- llama.cpp: https://github.com/ggml-org/llama.cpp
- Qwen3-ASR 官方: https://github.com/QwenLM/Qwen3-ASR
- ModelScope 模型: https://www.modelscope.cn/collections/Qwen/Qwen3-ASR
- OpenVINO 加速方案: https://www.modelscope.cn/learn/5558
