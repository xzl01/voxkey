# VoxKey on macOS — 双引擎本地 ASR（NCE + GPU）

本目录实现 macOS（Apple Silicon）上的本地语音识别，两条计算路径**并发运行、结果融合**：

| 引擎 | 模型 | 算力 | 框架 | 特点 |
| --- | --- | --- | --- | --- |
| **FunASR** | Paraformer / SenseVoice（非自回归） | **Apple Neural Engine (NCE/ANE)** | Core ML | 低延迟，整图一次前向，最适合 ANE |
| **Qwen3-ASR** | Qwen3-ASR（自回归语音大模型） | **Apple GPU** | llama.cpp Metal | 高精度、多语种，跑在 GPU 而非 NCE |

> 为什么 Qwen3-ASR 不走 NCE？它是逐 token 生成的自回归 LLM，动态解码循环 +
> 大模型权重无法被 ANE 调度（内存/算子都不支持）。只有 **Core ML** 能驱动 NCE，
> 而自回归解码必须用 GPU。FunASR 是**非自回归**模型，整张图一次前向，正好对 ANE 胃口。

## 架构

```
  麦克风 (AVAudioEngine)
        │  16kHz mono s16le
        ▼
   audio.py  ── 解码/重采样/去静音 (VAD) ──► 16k mono float32
        │
        ├──────────────┬───────────────────┐
        ▼              ▼                   │
  FunASRCoreML   Qwen3GPU            (两路并发)
  (Core ML/ANE)  (llama.cpp Metal)
        │              │
        └────► orchestrator.py (融合: fast_first / best) ◄──┘
                    │
            service.py  /  macos_daemon.py
            (/transcribe, /transcribe/stream, 全局热键+粘贴)
```

- **`audio.py`** — 唯一音频归一化点。ffmpeg 解码任意格式 → 16k 单声道；轻量能量
  VAD 去静音；`MicCapture` 通过 `capture_helper`(Swift/AVAudioEngine) 实时流式取帧。
- **`funasr_coreml.py`** — 用 **ONNX Runtime Core ML EP** 直接跑 SenseVoice 导出的
  ONNX 编码器（coremltools 9.0 已无 ONNX 前端，故不走 `.mlpackage`）；前端用
  FunASR 的 `WavFrontend`（80 维 fbank → lfr_m=7 堆叠成 560 维 → CMVN），
  输出 4 个输入喂编码器、对 `ctc_logits` 做 CTC 解码。
- **`qwen3_gpu.py`** — 复用项目自带的 `qwen_asr_gguf`（与 Linux daemon 同款），
  `llm_use_gpu=True` → llama.cpp 走 Metal GPU；加载 int4 GGUF（已量化）。
- **`orchestrator.py`** — 两个引擎分别线程并发推理，按策略融合：
  - `fast_first`（默认）：先出 NCE 结果（低感知延迟），GPU 结果回来后若更优则替换。
  - `best`：优先用 GPU 结果，失败回退 NCE。
- **`service.py`** — 接管原 `asr-service` 的 `/transcribe` 占位（501），提供
  `/health`、`/transcribe`、`/transcribe/stream`（SSE 实时流式）。
- **`macos_daemon.py`** — 全局热键（`hotkey_helper`）+ 实时录音 + 双引擎 + 剪贴板粘贴
  （IME 安全，中文正确上屏）。

## 环境配置与依赖（需求 1）

```bash
# 一键安装系统依赖 + Python venv + 模型转换/下载
bash setup.sh

# 或手动：
brew bundle --file=Brewfile
python3.11 -m venv .venv && . .venv/bin/activate   # 需要 Python 3.11/3.12（coremltools 要求 <3.13）
pip install -r requirements.txt
pip install llama-cpp-python gguf                 # GPU 解码器的 Metal 后端（见下）
```

- **NCE 工具链**：`onnxruntime`（Core ML EP 直接在 ANE 上跑 ONNX）+ `funasr`/`modelscope`
  （导出 SenseVoice ONNX）+ `sentencepiece`（生成词表）。**不再依赖 coremltools 的
  ONNX 前端**（coremltools ≥ 8 已移除），因此也不生成 `.mlpackage`。
- **GPU 工具链**：`qwen_asr_gguf` + **`llama-cpp-python`**（自带 Metal 后端，**无需手动构建
  llama.cpp**）。本目录的 `qwen-asr/qwen_asr_gguf/inference/llama.py` 已改为直接绑定
  `llama_cpp` 的 C-API（结构体/函数与编译版本一致），不再从 `inference/bin/` 加载预编译
  的 `libllama.dylib`。
- **系统框架**：AVFoundation / Core ML / Metal 随 macOS 提供；麦克风权限需在
  *系统设置 → 隐私与安全性 → 麦克风* 授权给运行服务的终端/App。

### 编译 Swift 辅助程序

```bash
swiftc -O capture_helper.swift  -o capture_helper    # 实时录音
swiftc -O hotkey_helper.swift   -o hotkey_helper     # 全局热键
```

## 模型获取（需求 2）

权重不入库（见仓库根 `.gitignore` 的 `models/`），首次由 `setup.sh` 自动拉齐：

```bash
bash back-end/platforms/macos/setup.sh
```

脚本会依次确保两个引擎的模型，**无需手动给 URL**：

* **FunASR（NCE / ANE）**：优先从 GitHub Release 下载预转换好的 `funasr_coreml` 包
  （`model.onnx` + `am.mvn` + `tokens.txt` + `frontend.json`），一键解压即用；
  若下载源不可达，则回退到本地 `torch`/`funasr` 导出（见下）。
* **Qwen3-ASR（GPU / Metal）**：从 HuggingFace
  `nzyaltair/Qwen3-ASR-0.6B-gguf` 下载 int4 编码器 ONNX + q4_k LLM GGUF，
  并自动用 `hf-mirror.com` 镜像回退。

### 可选：离线 / 自定义源

| 场景 | 做法 |
| --- | --- |
| 用自带预转换包 | `VOXKEY_FUNASR_URLS=https://.../funasr_coreml.tar.gz python ensure_funasr.py --out models/funasr_coreml`（URL 可直接是完整 `.tar.gz` 文件，也可填其所在目录；校验哈希取自仓库 `manifests/funasr_coreml.json`） |
| 用其它 Qwen3 镜像 | `VOXKEY_QWEN3_URLS=... VOXKEY_QWEN3_MIRRORS=... python ensure_qwen3.py --out models/qwen3_asr` |
| 跳过某引擎 | `SKIP_FUNASR_CONVERT=1` / `SKIP_QWEN_DOWNLOAD=1` 再跑 `setup.sh` |
| 纯本地转换 FunASR | `python convert_funasr_coreml.py --model iic/SenseVoiceSmall --out models/funasr_coreml --quantize int8`（跳过下载改用本地转换：`python ensure_funasr.py --out models/funasr_coreml --no-convert`） |

> FunASR 模型 id 必须是 `iic/SenseVoiceSmall`（短名 `sensevoice_small` 在 ModelScope 上 404）。
> 本地转换需要 `funasr` + `modelscope` + `torch` + `torchaudio` + `onnxscript` + `sentencepiece`；
> 脚本内部已强制 `torch.onnx.export(dynamo=False)`，以规避 torch≥2.13 dynamo 导出器在
> `onnx.version_converter` 上把 Pad 降到 opset 17 时崩溃的问题。

> Qwen3 文件名约定：`qwen3_asr_encoder_frontend.int4.onnx`、
> `qwen3_asr_encoder_backend.int4.onnx`、`qwen3_asr_llm.q4_k.gguf`（LLM 是 **q4_k** 而非
> int4），需与 `config.json` 的 `qwen3_gpu.llm_fn` 对应。加载走 `llama-cpp-python` 的 Metal 后端。

## 运行

```bash
# 服务（供桌面 UI / daemon 通过 HTTP 调用，替代原 501 占位）
python service.py
curl -F "data=@clip.wav" http://127.0.0.1:17863/transcribe

# 守护进程（全局热键录音 + 实时识别 + 粘贴上屏）
python macos_daemon.py --config config.json

# 诊断 / 基准测试（延迟、内存、计算单元）
python benchmark.py --wav sample.wav
```

## 测试集与单元测试

NCE 引擎用 AISHELL-1 测试集做准确性验证。为避免在仓库里塞入完整语料，只导入
一个**固定子集**做功能性验证，放在独立的 `test/` 目录（不放 `models/`）：

```bash
# 从本地 OpenSLR data_aishell 生成 50 条子集（strided 采样，覆盖全部测试说话人）
python _build_eval_set.py --source aishell1 --split test \
    --data-dir /Volumes/拓展盘/Dev/data_aishell --limit 50 \
    --out test/data/aishell1_zh
```

`test/test_nce.py` 是 pytest 功能测试：在子集上跑 NCE 引擎，断言输出非空且
CER < 20%（功能性闸门；真实 CER 约 0.3–2%）。

```bash
.venv/bin/python -m pytest test/test_nce.py -q -s
# 用 CPU 代替 ANE：NCE_COMPUTE=cpu .venv/bin/python -m pytest test/test_nce.py -q
```

如需完整基准（7176 条，约 1 小时），对完整集跑评估脚本：

```bash
python _nce_eval.py --compute ane --report out/zh_report.json
```

> 注：本地 `wav/<spk>.tar.gz` 内部已带 `train/|dev/|test/` 前缀，脚本按前缀筛分，
> 无需硬编码说话人 id。完整语料不入库，仅本机 `/Volumes/拓展盘/Dev/data_aishell` 存放。

## 量化与内存优化（需求 4）

- **FunASR (NCE)**：转换期 int8/fp16 训练后量化；Core ML 将量化权重保留在 ANE
  专用内存，显著降低占用并提升吞吐。
- **Qwen3-ASR (GPU)**：直接加载 int4 GGUF；Apple Silicon 统一内存让 GPU 与 CPU
  共享权重，免去显存拷贝。
- 两者均只加载一次并在进程内复用（warmup 在启动时完成）。
- 音频层先做 VAD 去静音，减少送入模型的无效计算。

## 验证 NCE 确实被使用

Core ML 公共 API 不暴露逐层 ANE 命中计数，可用 **Instruments** 验证：

```bash
xcrun xctrace record --template 'Metal System Trace' \
  --attach --pid $(pgrep -f service.py) --output trace.trace
```

在 `com.apple.neural.engine` 分类下查看 ANE 活动；或插入 Core ML `os_signpost`
区间做细粒度统计。

## 与现有代码的关系

- 原 `back-end/asr-service/main.py` 的 `/transcribe` 返回 501；本目录的 `service.py`
  是它的 macOS 功能实现，沿用同一端口（17863）与请求契约。
- `back-end/voice-daemon/voice_input_daemon.py` 的 `QwenAsr` 已能本地加载
  `qwen_asr_gguf`，本目录的 `qwen3_gpu.py` 复用相同加载方式。
