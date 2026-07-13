# Windows 本地大模型后端实施计划（云端模型离线替代）

> 目标：在 **Windows + AMD 集显/APU（Radeon 780M，共享系统内存）** 上，把
> **Qwen3-ASR + FunASR 双引擎** 本地大模型后端跑通，作为云端 ASR 的离线替代。
> 本文是面向运维/开发部署的实施手册，配套代码改动已落到 `back-end/platforms/windows/`。

---

## 1. 环境准备：系统及硬件基线要求

### 1.1 硬件基线

| 组件 | 最低要求 | 推荐（Radeon 780M 集显实测基线） |
| --- | --- | --- |
| CPU | x86-64，4 核 | 8 核（Ryzen 7x4xU 级），供 FunASR 前端与 Qwen3 CPU 回退 |
| GPU | 任意支持 Vulkan 1.1 + DX12 的 AMD GPU | AMD Radeon 780M（RDNA3，集成于锐龙 7040/8040 系列） |
| 系统内存 | 16 GB | **32 GB**（集显无独立显存，GGUF + ONNX 权重与桌面/浏览器共享物理内存） |
| 磁盘 | 8 GB 空闲 | 16 GB（模型 + llama.cpp 库 + venv） |
| 网络 | 首次需联网拉模型/依赖 | 离线后全程断网可用 |

**关键约束（共享内存）**：Radeon 780M 不自带显存，GPU 与 CPU 共用系统内存。
模型权重、KV cache、DirectML 工作集都从同一块内存分配。因此：
- 总量上需预留给 Windows/桌面 ~8–10 GB，留给 ASR 后端约 **16–20 GB 上限**。
- 量化必须保守（见 §3），并限制 Qwen3 上下文长度与线程数，避免 OOM 或系统卡死。

### 1.2 软件基线

| 项目 | 版本/要求 | 说明 |
| --- | --- | --- |
| 操作系统 | Windows 10 22H2 / Windows 11 21H2+ | 需 DX12 + WDDM 2.7+ |
| Python | CPython **3.12** | 本机用全路径 `C:\Users\xzl01\AppData\Local\Programs\Python\Python312\python.exe`，**勿用 `python`/`uv`**（uv trampoline 在受限卷上无法 spawn 子进程） |
| MSVC | VS Build Tools 2022 + Windows SDK | llama.cpp 预编译 DLL 一般无需本地编译；但部分 pip 包需它 |
| AMD 驱动 | Adrenalin 23.12+ | 自带 **Vulkan runtime**（Qwen3 llama.cpp 用）与 **DirectML**（FunASR 用，随系统 DX12 提供） |
| WebView2 | 随 Windows 10/11 提供 | 桌面壳依赖 |

### 1.3 驱动与运行时自检

```powershell
# 1) Python 全路径可用
& "C:\Users\xzl01\AppData\Local\Programs\Python\Python312\python.exe" --version

# 2) Vulkan 运行时（Qwen3 用）—— 装 Vulkan SDK 后用 vulkaninfo，或简易验证：
#    启动服务后 /health 会报告 qwen3_gpu compute=gpu 即说明 Vulkan 命中

# 3) DirectML（FunASR 用）—— 是 Windows 系统组件，只要 Win10 1709+ 且 DX12 驱动正常即可
#    启动服务后 /health 报告 funasr_directml compute=dml 即说明命中

# 4) 设备内存拓扑自查
wmic path Win32_VideoController get Name,AdapterRAM,DriverVersion
```

---

## 2. 后端部署：本地推理框架安装与配置（兼容 Windows）

部署三件套到 `back-end/platforms/windows/.venv`：

1. **llama.cpp（Vulkan 后端，预编译 DLL）** — 供 Qwen3-ASR decoder。
   - 在 Releases 下载带 Vulkan 的 Windows 包，把 `llama.dll` / `ggml-vulkan.dll`
     等放进 `qwen_asr_gguf/inference/bin/`（与 macOS/Linux 同级目录结构）。
   - **无需本地编译**（这是 Windows 相对 Arch Linux 的优势）。
2. **ONNX Runtime DirectML**（`onnxruntime-directml`）— 供 FunASR encoder。
   - `pip install onnxruntime-directml`，会话 provider 设为
     `["DmlExecutionProvider","CPUExecutionProvider"]`。
3. **FastAPI + uvicorn** — 本地 API 服务。

安装（受限于云同步卷，用全路径 python + 普通 pip，避免符号链接型包）：

```powershell
cd back-end/platforms/windows
& "C:\Users\xzl01\AppData\Local\Programs\Python\Python312\python.exe" -m venv .venv
& .venv\Scripts\python.exe -m pip install -r requirements.txt
# onnxruntime-directml / fastapi / uvicorn 已在 requirements.txt 中
```

> 若遇 `ECONNRESET`：多为防病毒「受控文件夹访问」拦截，把项目目录加入排除后重试。

服务启动（见 `run.ps1` / 直接起服务）：

```powershell
& .venv\Scripts\python.exe service.py        # 监听 127.0.0.1:17863
& .venv\Scripts\python.exe service.py --self-test
```

守护进程（热键录音 → 调本地服务）：

```powershell
.\run.ps1                  # 默认 asr_backend=http，指向 127.0.0.1:17863
```

---

## 3. 模型量化与加载：适合本地硬件的格式与策略

### 3.1 Qwen3-ASR（GGUF decoder + ONNX encoder）

| 文件 | 格式/量化 | 说明 | 780M 占用估算 |
| --- | --- | --- | --- |
| `qwen3_asr_llm.q4_k_m.gguf` | GGUF **Q4_K_M** | decoder 主权重（优先）；若内存吃紧退到 **Q4_K_S** | ~1.1 GB（1.7B）/ ~0.5 GB（0.6B） |
| `qwen3_asr_encoder_frontend.int4.onnx` | ONNX **int4** | encoder 前端，DirectML/CPU | ~数十 MB |
| `qwen3_asr_encoder_backend.int4.onnx` | ONNX **int4** | encoder 后端 | ~数十 MB |

- decoder 走 **Vulkan**（llama.cpp），encoder 的 `onnx_provider` 由
  `selected_runtime_id` 决定：`gpu-directml` → `"Dml"`，否则 `"CPU"`。
- 受共享内存约束，`llm_use_gpu=True` 时上下文长度（n_ctx）控制在 **512–1024**，
  `num_threads` 设为物理核数的一半（给系统留余量）。

### 3.2 FunASR / SenseVoice（ONNX + DirectML）

| 文件 | 格式/量化 | 说明 |
| --- | --- | --- |
| `model.onnx` | ONNX **int8**（优先）/ **int4** | SenseVoice encoder，经 `convert_funasr_coreml.py` 同款导出 + 量化 |
| `tokens.txt` | 词表 | CTC 解码用 |
| `am.mvn` + `frontend.json` | 前端配置 | WavFrontend（80-dim fbank → LFR 560-dim + CMVN） |

- DirectML 把 encoder 派发到 Radeon 780M（DX12）；剩余 op 自动回退 CPU。
- int8 在集显上精度/速度均衡；若内存极紧可退 int4（精度略降）。

### 3.3 内存占用上限建议（Radeon 780M，32 GB 机）

| 进程 | 常驻 | 峰值 |
| --- | --- | --- |
| Qwen3 decoder (Vulkan) + encoder | ~1.5 GB | ~2.5 GB |
| FunASR DirectML + 前端 | ~0.4 GB | ~0.8 GB |
| FastAPI/uvicorn（workers=1） | ~0.2 GB | ~0.5 GB |
| **合计** | **~2.1 GB** | **~3.8 GB**（留足系统余量） |

---

## 4. API 接口集成：与现有应用对接的本地 API

`service.py` 是双引擎 FastAPI 服务，沿用桌面壳既有契约（与 macOS `service.py` 同构）：

| 端点 | 方法 | 用途 | 返回 |
| --- | --- | --- | --- |
| `/health` | GET | 存活 + 已加载引擎 | `{"ok":true,"service":"voxkey-asr-win","engines":[{"kind":...,"compute":"dml"/"gpu"}]}` |
| `/transcribe` | POST | raw 音频字节 → 融合转写 | `{"text":...,"chosen_engine":...,"total_latency_s":...,"funasr":...,"qwen3":...}` |
| `/transcribe/stream` | GET | SSE 实时转写 | `text/event-stream` |
| `/engines` | GET | 各引擎存在/启用/加载态 | 列表 |
| `/engines` | POST | 热插拔（更新 `config.json` 后重建 orchestrator） | 列表 |

**与守护进程的对接**（契约不变，零回归）：
- `windows_daemon.py` 的 `ASR` 类 `asr_backend="http"` 时，POST `file` 表单字段到
  `asr_service_url+/transcribe`，读取 `{"text": ...}`。
- `asr_fallback_local=true` 时，HTTP 失败回退到本地双引擎（local 模式亦支持双引擎并行加载）。
- `selected_runtime_id="gpu-directml"` 时，Qwen3 encoder 切到 `Dml` provider。

`config.example.json` 新增 `engines` 段示例：

```json
"engines": {
  "qwen3_gpu":   { "enabled": true,  "model_dir": "models/qwen3_asr", "llm_fn": "qwen3_asr_llm.q4_k_m.gguf", "onnx_provider": "Dml", "llm_use_gpu": true },
  "funasr_directml": { "enabled": true, "model_path": "models/funasr_directml", "compute_units": "dml" }
},
"selected_runtime_id": "gpu-directml"
```

---

## 5. 测试与调优：速度 / 并发 / 显存指标与优化

### 5.1 验证指标

| 指标 | 目标（780M 集显） | 测量方式 |
| --- | --- | --- |
| 首字延迟（FunASR） | < 300 ms | `funasr.latency_s` |
| 整句延迟（Qwen3 精修） | < 1.5 s（≤10 s 音频） | `qwen3.latency_s` |
| 融合总延迟 | < 1.5 s | `total_latency_s` |
| 峰值内存占用 | < 4 GB | 任务管理器 / `wmic` |
| 并发请求 | 4 路串行无错（uvicorn workers=1） | 压测脚本循环 POST |
| 准确率 | CER 接近云端基线 | 固定测试集对比 |

### 5.2 自测命令

```powershell
# 服务存活 + 引擎加载
curl http://127.0.0.1:17863/health

# 转写一段 16k 单声道 wav
curl -F "file=@sample.wav" http://127.0.0.1:17863/transcribe

# 引擎状态
curl http://127.0.0.1:17863/engines

# 守护进程自检（列出设备/依赖）
.\run.ps1 --self-test
```

### 5.3 优化方向（AMD 集显/共享内存特化）

1. **量化下探**：Qwen3 由 Q4_K_M 退到 Q4_K_S / Q3_K 进一步省内存；FunASR 由 int8 退 int4。
2. **`GGML_VK_DISABLE_F16`**：该开关原为 Intel iGPU FP16 溢出加。RDNA3 的 Vulkan FP16
   一般稳定，**建议实测对比后关闭**（可能更快）；若出 NaN/乱码再打开。
3. **并发模型**：uvicorn `workers=1` + 内部双引擎线程并行（orchestrator 已做）；
   多 worker 会让 GGUF/ONNX 各占一份内存，**集显下不推荐**。
4. **线程与 n_ctx**：`num_threads` 设物理核一半，`n_ctx` ≤ 1024，避免与系统争用。
5. **DirectML 线程**：通过 `OMP_NUM_THREADS` / `ORT_THREAD_POOL` 限制 encoder CPU 回退线程。
6. **快速首字**：fusion `mode="fast_first"`（默认），FunASR 先出结果，Qwen3 落地后精修。

---

## 附：目录改动一览

```
back-end/platforms/windows/
├── funasr_directml.py   # [NEW] DirectML EP 版 SenseVoice（仿 funasr_coreml.py）
├── orchestrator.py      # [NEW] 从 macOS 复制 DualEngineOrchestrator + FusionConfig
├── common.py            # [NEW] EngineKind/Transcript 等共享类型（FUNASR 改名 directml）
├── service.py           # [NEW] 双引擎 FastAPI 服务（移植 macOS service.py）
├── windows_daemon.py    # [MODIFY] selected_runtime_id → Dml；local 模式双引擎
├── config.example.json  # [MODIFY] 增加 engines 段
├── requirements.txt     # [MODIFY] + fastapi/uvicorn/onnxruntime-directml
├── run.ps1              # [MODIFY] 启动服务参数 + 线程约束
└── README.md            # [MODIFY] 本地后端章节
docs/windows-local-llm-backend.md  # 本文
```
