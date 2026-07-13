# VoxKey on Windows

本目录实现 Windows 上的本地语音输入守护进程：全局热键录音（WASAPI）→ 调用
ASR 后端转写 → 通过剪贴板 + `Ctrl+V` 上屏（IME 安全，中文正确输入）。

它是 `back-end/platforms/macos/macos_daemon.py` 和
`back-end/voice-daemon/voice_input_daemon.py` 的 Windows 对应实现，仅依赖
跨平台 Python 库，不引入 Linux-only 模块（evdev / fcntl）。

## 组件

| 文件 | 作用 |
| --- | --- |
| `windows_daemon.py` | 守护进程：热键、录音、ASR、上屏、通知 |
| `config.example.json` | 配置样例（`trigger` / `capture` / `asr_*`） |
| `requirements.txt` | Python 依赖 |
| `setup.ps1` | 环境引导：装 MSVC / Python / ffmpeg、建 venv、装依赖 |
| `run.ps1` | 启动脚本：激活 venv 并运行守护进程 |

## 1. 系统依赖（一次性）

Tauri v2 桌面应用需要 **MSVC 构建工具 + Windows SDK**，Python 守护进程需要
**CPython 3.12**。本机已确认 `uv` 的 trampoline 在受限用户卷上无法 spawn
Python 子进程，因此用 winget 安装独立 CPython，而非依赖 `uv python`。

一键引导（需要管理员权限以安装 VS Build Tools，会弹出 UAC）：

```powershell
powershell -ExecutionPolicy Bypass -File back-end/platforms/windows/setup.ps1
```

脚本会：

1. 安装 Visual Studio Build Tools 2022 + Windows 10/11 SDK（MSVC）。
2. 安装 Python 3.12（若缺失）。
3. 安装 ffmpeg（可选）。
4. 在 `back-end/platforms/windows/.venv` 创建 venv 并安装 `requirements.txt`。

> WebView2 运行时在 Windows 10/11 上通常已随系统提供；若缺失，从
> https://developer.microsoft.com/microsoft-edge/webview2/ 安装。

## 2. 配置

```powershell
cd back-end/platforms/windows
Copy-Item config.example.json config.json
```

编辑 `config.json`：

```json
{
  "trigger": {
    "enabled": true,
    "backend": "win32",
    "key": "right_shift",
    "mode": "hold"
  },
  "asr_backend": "http",
  "asr_service_url": "http://127.0.0.1:17863"
}
```

- `trigger.key`：热键名，传给 `keyboard` 库（如 `right_shift`、`right ctrl`、
  `f4`、`space`）。也可填虚拟键码数字。
- `trigger.mode`：`hold`（按住录音，松开转写）或 `toggle`（按一次开始，再按结束）。
- `asr_backend`：`http`（POST 音频到 `asr_service_url/transcribe`）或
  `local`（加载本地 Qwen3-ASR；需 Windows 版 llama.cpp + ONNX Runtime，见下）。

> 默认安全：`config.example.json` 里 `trigger.enabled` 为 `false`，守护进程
> 不监听任何按键。

## 3. 运行

```powershell
.\run.ps1                 # 使用同目录 config.json
.\run.ps1 --self-test     # 仅自检（列出音频设备、依赖可用性）
.\run.ps1 --transcribe-file clip.wav   # 转写一个已有 WAV
```

`run.ps1` 会自动解析 `.venv` 里的 Python，并设置 `GGML_VK_DISABLE_F16=1`。

## 4. ASR 后端

### HTTP（默认，推荐先跑通）

启动任意实现了 `/transcribe`（接收 `file` 表单字段、返回 `{"text": "..."}`）
的 ASR 服务，例如 macOS 目录的 `service.py`（跨平台可用）：

```powershell
# 在装好依赖的 venv 中
python back-end/platforms/macos/service.py
```

然后守护进程把录音 POST 过去。

### 本地双引擎（local / 离线首选）

Windows 本地后端是 **Qwen3-ASR（llama.cpp Vulkan）+ FunASR（ONNX Runtime
DirectML）双引擎**，由 `service.py` 暴露 `/transcribe` API，`windows_daemon.py`
在 `asr_backend="http"` 时调用它；`asr_backend="local"` 时守护进程直接加载同一
套引擎（共享 `service.load_engines`，二者不会分叉）。

硬件目标：**AMD 集显/APU（Radeon 780M，共享系统内存）**。两者都跑在同一块
GPU 上——Qwen3 decoder 走 Vulkan（AMD Adrenalin 自带 Vulkan runtime），FunASR
encoder 走 DirectML（DX12，任何 Windows GPU 都支持）。

准备步骤（推荐直接跑一键引导，见下）：

1. 安装 AMD Adrenalin 驱动（含 Vulkan + DX12/DirectML）。
2. 拉取 Qwen3-ASR-GGUF 项目，把带 **Vulkan** 的预编译 llama.cpp DLL
   （`llama.dll` / `ggml-vulkan.dll`）放进
   `<asr_project_dir>/qwen_asr_gguf/inference/bin/`（Windows 无需本地编译）。
   **这些 Vulkan DLL 已随本项目发布 `voxkey-models-v1` 提供**
   （资产名 `llama-b9940-bin-win-vulkan-x64.zip`），`bootstrap_assets.py` 会
   **自动优先从该 release 拉取**，无需手动下载 llama.cpp 官方包、也不再受
   官方源限速影响。
3. 下载 Qwen3-ASR GGUF（`qwen3_asr_llm.q4_k_m.gguf`，显存紧可退 `q4_k_s`）与
   int4 ONNX encoder；导出/下载 FunASR（SenseVoice）`model.onnx`（int8 优先，
   int4 备选）及 `tokens.txt` / `am.mvn` / `frontend.json`。
4. 在 `config.json` 的 `engines` 段填写模型路径，并设
   `selected_runtime_id: "gpu-directml"`（让 Qwen3 encoder 也上 GPU）。

> 一键引导（克隆 Qwen3-ASR-GGUF、拉 Qwen3 权重、拉 Vulkan DLL、写回 config）：
> ```powershell
> & .venv\Scripts\python.exe bootstrap_assets.py --skip-dll   # 已手工放好 DLL 时
> & .venv\Scripts\python.exe bootstrap_assets.py --export-funasr   # 额外导出 FunASR DirectML ONNX
> ```
> 完事后直接 `& .venv\Scripts\python.exe run_backend.py`（**不要**加 `--skip-funasr`）
> 启动双引擎服务。

启动：

```powershell
# 启动本地 API 服务（:17863）+ 守护进程
.\run.ps1 -Service
# 或仅起服务做自测
& .venv\Scripts\python.exe service.py
curl http://127.0.0.1:17863/health
curl -F "file=@sample.wav" http://127.0.0.1:17863/transcribe
```

`asr_fallback_local` 为 `true` 时，HTTP 失败会自动回退到本地引擎。更完整的
部署/量化/调优说明见仓库根 `docs/windows-local-llm-backend.md`。

## 5. 故障排查

- **热键不生效 / 权限**：`keyboard` 库在部分 Windows 配置下需要以管理员身份
  运行守护进程（右键 PowerShell → 以管理员身份运行）。
- **录音无声**：检查默认输入设备；可在 `config.json` 的 `capture.device` 指定
  WASAPI 设备名或索引（`run.ps1 --self-test` 会列出设备）。
- **/health 只有 qwen3_gpu 没有 funasr_directml**：通常是缺
  `onnxruntime-directml` 或 `models/funasr_directml/model.onnx` 缺失。装依赖 /
  放模型后重启服务。
- **Qwen3 未走 GPU（compute=cpu）**：检查 Vulkan runtime（Adrenalin 驱动）与
  `qwen_asr_gguf/inference/bin/` 下的 `ggml-vulkan.dll` 是否存在。
- **共享内存 OOM / 系统卡顿**：Radeon 780M 无独立显存。退量化（Qwen3 → Q4_K_S、
  FunASR → int4），或在 `run.ps1` 降低 `OMP_NUM_THREADS`，并把 Qwen3 `n_ctx`
  控制到 1024 以内。
- **双引擎同时上 GPU 触发「GPU 设备已被移除」（DXGI 887A0005 / 80004005，
  `GetDeviceRemovedReason`）**：Radeon 780M 是共享系统内存的 APU，Vulkan(Qwen3)
  与 DirectML(FunASR) 同时占 GPU 显存会把共享显存压爆，Windows TDR 直接重置 GPU，
  表现为 `/transcribe` 两个引擎都 `ok:false`、但服务不崩（每个引擎独立 try）。
  **本机实测结论：共享显存 APU 不要双引擎同跑**。任选其一即可：
  - 只用 Qwen3（推荐，更准确）：`config.json` 里 `engines.funasr_directml.enabled`
    设为 `false`；
  - 只用 FunASR（更轻）：把 `funasr_directml.enabled` 设 `true`、`compute_units`
    设 `cpu`（走 CPU，不与 Qwen3 抢 GPU），并把 `qwen3_gpu.enabled` 设 `false`。
  改完重启 `run_backend.py`（不带 `--skip-funasr`）。
- **依赖安装失败（unknown error / os error 448）**：用户目录若位于云同步或受
  “受控文件夹访问”/防病毒保护的卷，pnpm 与 uv 的符号链接操作会失败。把项目
  目录加入防病毒排除，或将 CPython 装到普通路径（如 `C:\Python312`，
  `setup.ps1` 会自动识别）。
- **npm 包下载 ECONNRESET**：registry.npmjs.org 连接被重置，多为代理/防火墙。
  配置 npm 镜像或公司代理后重跑 `pnpm install`。
- **Tauri 编译报找不到 MSVC / Windows SDK**：确认 `setup.ps1` 第 1 步已成功，
  或手动安装“使用 C++ 的桌面开发”工作负载 + Windows 10/11 SDK。
