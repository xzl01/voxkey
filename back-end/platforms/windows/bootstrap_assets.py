#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
r"""下载并校验 VoxKey Windows 本地双引擎 ASR 所需的外部资源。

覆盖四块资源：
  1. Qwen3-ASR-GGUF 项目（提供 `qwen_asr_gguf` Python 推理包）— git clone
  2. Qwen3-ASR 权重（q4_k GGUF 解码器 + int4 ONNX 编码器）— 项目发布 voxkey-models-v1
  3. llama.cpp Vulkan 动态库（llama.dll / ggml-vulkan.dll）— llama.cpp release zip
  4. FunASR / SenseVoice ONNX（model.onnx + 前端资产）— 用已装的 funasr 导出（--export-funasr）

其中第 2 项的权重与 macOS 端**完全一致、跨平台通用**：直接取自本项目自己的发布
`voxkey-models-v1`（该 release 的 Qwen3 权重为 0.6B，镜像自 nzyaltair/Qwen3-ASR-0.6B-gguf）。
GGUF / ONNX 仅是序列化张量，不含平台特定字节，因此 Windows 复用 macOS 同一份权重，
仅推理运行时不同（Windows 走 llama.cpp Vulkan + ONNX Runtime DirectML；macOS 走 Metal + CoreML）。
FunASR 则因 macOS 用 CoreML(ANE)、Windows 用 DirectML，无法共用，故 Windows 端本地导出 int8 ONNX。

用法（用 venv 解释器运行，funasr/torch 才在环境里）：
  & .venv\Scripts\python.exe bootstrap_assets.py                   # 下载全部（不含 FunASR 导出）
  & .venv\Scripts\python.exe bootstrap_assets.py --check           # 只看缺什么
  & .venv\Scripts\python.exe bootstrap_assets.py --skip-clone      # 已克隆过则跳过
  & .venv\Scripts\python.exe bootstrap_assets.py --skip-dll        # 跳过 Vulkan DLL（已手工放好）
  & .venv\Scripts\python.exe bootstrap_assets.py --export-funasr   # 上面 + 导出 FunASR DirectML ONNX

完成后会把 asr_project_dir / engines 的绝对路径写回 config.json，直接跑 run_backend.py 即可。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"

# ---- 资源来源 ----------------------------------------------------------------
# Qwen3-ASR 权重（GGUF 解码器 + int4 ONNX 编码器）直接取自本项目自己的发布
# voxkey-models-v1，与 macOS 端完全一致（权重格式跨平台，无需分别打包）。
# 注意：该 release 的 Qwen3 权重为 0.6B（镜像自 nzyaltair/Qwen3-ASR-0.6B-gguf）。
# Qwen3 权重优先从 hf-mirror.com 拉（国内镜像，速度快，且沿用 macOS 的
# HF_MIRROR 回退思路）；GitHub 官方发布与 gh-proxy/ghfast 作为兜底。
HF_MIRROR_BASE = "https://hf-mirror.com/nzyaltair/Qwen3-ASR-0.6B-gguf/resolve/main"
GITHUB_RELEASE = "https://github.com/xzl01/voxkey/releases/download/voxkey-models-v1"
QWEN3_SOURCES = [
    HF_MIRROR_BASE,
    GITHUB_RELEASE,
    "https://gh-proxy.com/https://github.com/xzl01/voxkey/releases/download/voxkey-models-v1",
    "https://ghfast.top/https://github.com/xzl01/voxkey/releases/download/voxkey-models-v1",
]
# 三个 Qwen3 权重文件 -> 期望字节数（用于下载后完整性校验，release 资产固定）。
QWEN3_FILES = {
    "qwen3_asr_llm.q4_k.gguf": 484_215_360,
    "qwen3_asr_encoder_frontend.int4.onnx": 20_343_991,
    "qwen3_asr_encoder_backend.int4.onnx": 94_750_816,
}

# llama.cpp 带 Vulkan 的 Windows 预编译包（Qwen3 的 GGUF 解码器在 AMD 上走
# Vulkan）。注意资产名带 -x64 后缀（如 llama-b9940-bin-win-vulkan-x64.zip），
# 老 tag b9106 的 win-vulkan 资产已 404。
LLAMACPP_TAG = "b9940"
LLAMACPP_VULKAN_ZIP = (
    f"https://github.com/ggml-org/llama.cpp/releases/download/"
    f"{LLAMACPP_TAG}/llama-{LLAMACPP_TAG}-bin-win-vulkan-x64.zip"
)
# Windows 专属的 Vulkan DLL 包也镜像进本项目发布 voxkey-models-v1（与 Qwen3 权重
# 同 release），作为首选来源：这样 Windows 端不再依赖被限速的 llama.cpp 官方源，
# 且资产随 release 版本受控。上传文件名必须与 LLAMACPP_VULKAN_ZIP 的基名一致。
VULKAN_SOURCES = [
    f"{GITHUB_RELEASE}/llama-{LLAMACPP_TAG}-bin-win-vulkan-x64.zip",
    LLAMACPP_VULKAN_ZIP,
    f"https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/releases/download/"
    f"{LLAMACPP_TAG}/llama-{LLAMACPP_TAG}-bin-win-vulkan-x64.zip",
]
FUNSAR_MODEL = "iic/SenseVoiceSmall"

# 默认把 Qwen3-ASR-GGUF 克隆到本目录下的 qwen-asr/（提供 qwen_asr_gguf 推理包）。
DEFAULT_ASR_DIR = HERE / "qwen-asr"
# Qwen3 权重落地目录（与 macOS 的 models/qwen3_asr 对齐）。
QWEN3_DIR = HERE / "models" / "qwen3_asr"


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def run(cmd, **kw) -> int:
    log("> " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, **kw).returncode


def _is_valid_zip(p: Path) -> bool:
    """粗略校验 .zip 是否完整（防止 ECONNRESET 下截断的伪成品被误用）。"""
    if not p.is_file() or p.stat().st_size == 0:
        return False
    if p.suffix.lower() != ".zip":
        return True
    try:
        with zipfile.ZipFile(p, "r") as z:
            return z.testzip() is None
    except Exception:
        return False


def download(url: str, dest: Path, retries: int = 5) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 已存在且校验为完整 zip 的成品直接复用，避免网络抖动下重复下载大文件。
    if _is_valid_zip(dest):
        log(f"已存在且完整，跳过下载：{dest.name}（{dest.stat().st_size >> 20} MB）")
        return True
    for attempt in range(1, retries + 1):
        # -C - 断点续传：若此前已被别的进程下了一部分，从这里接着下。
        proc = subprocess.run(
            ["curl.exe", "-L", "-f", "-C", "-", "--retry", "5", "--retry-delay", "3",
             "--retry-all-errors", "--connect-timeout", "20", "-m", "1800",
             "-o", str(dest), url],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if proc.returncode == 0 and _is_valid_zip(dest):
            return True
        # 已写出部分内容：curl 中断则保留以便 -C - 续传；仅当 curl 报告成功
        # 却仍损坏（极少见）时才删了重下，避免丢掉好不容易下到的进度。
        if dest.is_file() and dest.stat().st_size > 0 and not _is_valid_zip(dest):
            if proc.returncode == 0:
                log(f"下载完成但校验失败，删除损坏文件重下：{dest.name}")
                dest.unlink(missing_ok=True)
            else:
                err = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else ""
                log(f"下载中断（退出码 {proc.returncode}，第 {attempt}/{retries} 次），"
                     f"保留 {dest.stat().st_size >> 20} MB 续传：{err}")
        else:
            err = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else ""
            log(f"下载失败（第 {attempt}/{retries} 次）：{err}")
            if dest.is_file() and dest.stat().st_size == 0:
                dest.unlink(missing_ok=True)  # 清掉空文件，避免误导 --check / 重跑
        time.sleep(2)
    return False


def download_file(name: str, dest: Path, expected_size: int | None = None,
                  sources: list[str] | None = None, retries: int = 6) -> bool:
    """下载单个权重文件，带镜像回退 + 断点续传 + 大小校验。

    对齐 macOS `_fetch.py` 的行为：依次尝试 ``sources`` 里每个基址（基址/文件名）。
    下载完成后若已知期望字节数则校验大小，不符则删除重下（防止被截断的伪成品）。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and (expected_size is None or dest.stat().st_size == expected_size):
        log(f"已存在且大小正确，跳过：{name}（{dest.stat().st_size >> 20} MB）")
        return True
    src_list = sources or [GITHUB_RELEASE]
    urls = [f"{s.rstrip('/')}/{name}" for s in src_list]
    last_err = ""
    for attempt in range(1, retries + 1):
        for url in urls:
            proc = subprocess.run(
                ["curl.exe", "-L", "-f", "-C", "-", "--retry", "5", "--retry-delay", "3",
                 "--retry-all-errors", "--connect-timeout", "20", "-m", "1800",
                 "-o", str(dest), url],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            if proc.returncode == 0 and dest.is_file():
                if expected_size is None or dest.stat().st_size == expected_size:
                    return True
                log(f"大小不符（期望 {expected_size}，实际 {dest.stat().st_size}），删除重试：{name}")
                dest.unlink(missing_ok=True)
            else:
                last_err = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else ""
                log(f"下载失败：{url}（{last_err}）")
        log(f"第 {attempt}/{retries} 轮未成功，稍后重试…")
        time.sleep(2)
    log(f"下载失败：{name}（{last_err}）")
    return False


def unzip(zip_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(out_dir)
    log(f"解压 -> {out_dir}")


def gather_dlls(src_tree: Path, dst_dir: Path) -> None:
    """把解压树里所有 .dll 收拢到 dst_dir（llama.cpp zip 可能带 bin/ 子目录）。"""
    dst_dir.mkdir(parents=True, exist_ok=True)
    dlls = list(src_tree.rglob("*.dll"))
    if not dlls:
        raise RuntimeError(f"Vulkan zip 中未找到任何 .dll（检查 URL/ tag：{LLAMACPP_VULKAN_ZIP}）")
    for d in dlls:
        shutil.copyfile(d, dst_dir / d.name)
    log(f"已复制 {len(dlls)} 个 DLL -> {dst_dir}")


# --------------------------------------------------------------------------- #
# 各资源步骤
# --------------------------------------------------------------------------- #
def step_clone(asr_dir: Path, skip: bool) -> None:
    if skip:
        log("跳过克隆（--skip-clone）")
        return
    if (asr_dir / "qwen_asr_gguf").is_dir():
        log(f"Qwen3-ASR-GGUF 已存在：{asr_dir}")
        return
    asr_dir.parent.mkdir(parents=True, exist_ok=True)
    if run(["git", "clone", "--depth", "1", QWEN3_REPO_URL, str(asr_dir)]) != 0:
        raise RuntimeError("git clone 失败（确认已装 git 且网络可达）")
    log("已克隆 Qwen3-ASR-GGUF")


QWEN3_REPO_URL = "https://github.com/HaujetZhao/Qwen3-ASR-GGUF.git"


def step_gguf(skip: bool) -> Path:
    """下载 Qwen3-ASR 0.6B 权重（q4_k GGUF + int4 ONNX）到 models/qwen3_asr。

    权重来自本项目发布 voxkey-models-v1，与 macOS 完全一致、跨平台通用。
    """
    if skip:
        log("跳过 Qwen3 权重（--skip-gguf）")
        return QWEN3_DIR
    missing = [n for n in QWEN3_FILES if not (QWEN3_DIR / n).is_file()]
    if not missing:
        log(f"Qwen3 权重已齐备：{QWEN3_DIR}")
        return QWEN3_DIR
    log(f"下载 Qwen3 权重（共 {len(missing)} 个文件）：{', '.join(missing)}")
    for name, size in QWEN3_FILES.items():
        if not download_file(name, QWEN3_DIR / name, expected_size=size, sources=QWEN3_SOURCES):
            raise RuntimeError(f"Qwen3 权重下载失败：{name}")
    log(f"Qwen3 权重就绪：{QWEN3_DIR}")
    return QWEN3_DIR


def step_dll(asr_dir: Path, skip: bool) -> None:
    if skip:
        log("跳过 DLL（--skip-dll）")
        return
    bin_dir = asr_dir / "qwen_asr_gguf" / "inference" / "bin"
    if (bin_dir / "ggml-vulkan.dll").is_file() and (bin_dir / "llama.dll").is_file():
        log("Vulkan DLL 已存在，跳过")
        return
    zip_path = HERE / "_llamacpp_vulkan.zip"
    ok = False
    for url in VULKAN_SOURCES:
        if download(url, zip_path):
            ok = True
            break
        # 某来源失败：清掉半成品，再试下一个（避免把别的源的部分文件当成品）。
        if zip_path.is_file():
            zip_path.unlink(missing_ok=True)
    if not ok:
        raise RuntimeError(
            f"llama.cpp Vulkan 包下载失败（已尝试 {len(VULKAN_SOURCES)} 个来源）\n"
            f"（若 tag 不匹配，改 bootstrap_assets.py 顶部的 LLAMACPP_TAG；或把 "
            f"llama-{LLAMACPP_TAG}-bin-win-vulkan-x64.zip 上传到 voxkey-models-v1）"
        )
    tmp = HERE / "_llamacpp_unzip"
    if tmp.exists():
        shutil.rmtree(tmp)
    unzip(zip_path, tmp)
    gather_dlls(tmp, bin_dir)
    shutil.rmtree(tmp, ignore_errors=True)
    zip_path.unlink(missing_ok=True)


def step_funasr(out_dir: Path) -> None:
    """用已装的 funasr 把 SenseVoiceSmall 导出为 int8 ONNX（DirectML 校验）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    # 复用 macOS 导出脚本里的导出 + 前端资产拷贝逻辑（仅校验换成 DirectML）。
    sys.path.insert(0, str(HERE))
    from convert_funasr_coreml import export_onnx, _copy_frontend_assets
    import funasr  # noqa: F401
    import onnx
    import onnxruntime as ort
    from onnxruntime.quantization import quantize_dynamic, QuantType

    log(f"导出 FunASR ONNX（模型 {FUNSAR_MODEL}，需联网拉取权重，可能较慢）…")
    onnx_path = export_onnx(FUNSAR_MODEL, out_dir)  # 返回导出的 float32 model.onnx 路径
    # 拷贝前端资产（am.mvn / tokens.txt / frontend.json）；重新加载以读取模型配置
    model = funasr.AutoModel(model=FUNSAR_MODEL, disable_update=True)
    _copy_frontend_assets(model, out_dir)

    # 动态量化到 int8，并用 DirectML EP 校验可加载
    tmp = out_dir / "_qtmp.onnx"
    quantize_dynamic(str(onnx_path), str(tmp), weight_type=QuantType.QInt8)
    validated = onnx.load(str(tmp))
    onnx.checker.check_model(validated)
    onnx.save(validated, str(out_dir / "model.onnx"))
    tmp.unlink(missing_ok=True)
    sess = ort.InferenceSession(
        str(out_dir / "model.onnx"),
        providers=["DmlExecutionProvider", "CPUExecutionProvider"],
    )
    log(f"FunASR ONNX 就绪（DirectML 校验通过）：{out_dir / 'model.onnx'}")
    log(f"  inputs={[i.name for i in sess.get_inputs()]} outputs={[o.name for o in sess.get_outputs()]}")


# --------------------------------------------------------------------------- #
# 校验 + 写回配置
# --------------------------------------------------------------------------- #
def verify(asr_dir: Path, model_dir: Path, funasr_dir: Path, funasr_exported: bool) -> bool:
    log("资产校验：")
    ok = True

    def check(label: str, p: Path, must_exist: bool = True) -> None:
        nonlocal ok
        good = p.is_file()
        if must_exist and not good:
            ok = False
        log(f"  [{'OK ' if good else 'MISS'}] {label}: {p}")

    check("qwen_asr_gguf 包", asr_dir / "qwen_asr_gguf" / "__init__.py")
    check("Qwen3 GGUF", next(model_dir.glob("qwen3_asr_llm*.gguf"), model_dir / "qwen3_asr_llm.q4_k.gguf"))
    check("Qwen3 enc-front", model_dir / "qwen3_asr_encoder_frontend.int4.onnx")
    check("Qwen3 enc-back", model_dir / "qwen3_asr_encoder_backend.int4.onnx")
    check("ggml-vulkan.dll", asr_dir / "qwen_asr_gguf" / "inference" / "bin" / "ggml-vulkan.dll")
    check("llama.dll", asr_dir / "qwen_asr_gguf" / "inference" / "bin" / "llama.dll")
    if funasr_exported:
        check("FunASR model.onnx", funasr_dir / "model.onnx")
        check("FunASR tokens.txt", funasr_dir / "tokens.txt")
        check("FunASR am.mvn", funasr_dir / "am.mvn")
    else:
        log("  [SKIP] FunASR 未导出（未加 --export-funasr）")
    return ok


def update_config(asr_dir: Path, model_dir: Path, funasr_dir: Path, funasr_exported: bool) -> None:
    if not CONFIG.is_file():
        log("config.json 不存在，跳过写回（先跑一次 run_backend.py 生成）")
        return
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg["asr_project_dir"] = str(asr_dir)
    eng = cfg.setdefault("engines", {})
    q = eng.setdefault("qwen3_gpu", {})
    gguf = next(model_dir.glob("qwen3_asr_llm*.gguf"), None)
    q["model_dir"] = str(model_dir)
    if gguf is not None:
        q["llm_fn"] = gguf.name
    q["onnx_provider"] = "Dml"  # 与 selected_runtime_id=gpu-directml 一致
    q["enabled"] = True
    f = eng.setdefault("funasr_directml", {})
    if funasr_exported:
        f["model_path"] = str(funasr_dir)
    # 未导出时显式关闭，避免服务默认按启用去加载而报“缺失”错误。
    f["enabled"] = funasr_exported
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"已写回 config.json（asr_project_dir / engines 绝对路径）")


def main() -> int:
    ap = argparse.ArgumentParser(description="下载并校验 VoxKey Windows 本地 ASR 资源")
    ap.add_argument("--check", action="store_true", help="仅校验，不下载")
    ap.add_argument("--asr-dir", type=str, default=str(DEFAULT_ASR_DIR), help="Qwen3-ASR-GGUF 克隆目标")
    ap.add_argument("--skip-clone", action="store_true")
    ap.add_argument("--skip-gguf", action="store_true")
    ap.add_argument("--skip-dll", action="store_true")
    ap.add_argument("--export-funasr", action="store_true", help="额外导出 FunASR ONNX（联网拉 SenseVoice 权重）")
    args = ap.parse_args()

    asr_dir = Path(args.asr_dir).expanduser().resolve()
    funasr_dir = HERE / "models" / "funasr_directml"

    if args.check:
        verify(asr_dir, QWEN3_DIR, funasr_dir, args.export_funasr)
        return 0

    log(f"目标目录：asr_project_dir = {asr_dir}；Qwen3 权重 = {QWEN3_DIR}")
    step_clone(asr_dir, args.skip_clone)
    model_dir = step_gguf(args.skip_gguf)
    step_dll(asr_dir, args.skip_dll)
    funasr_exported = False
    if args.export_funasr:
        step_funasr(funasr_dir)
        funasr_exported = True

    all_ok = verify(asr_dir, model_dir, funasr_dir, funasr_exported)
    update_config(asr_dir, model_dir, funasr_dir, funasr_exported)
    if all_ok:
        (HERE / ".bootstrap_done").write_text(
            f"ok\nqwen3={model_dir}\nasr={asr_dir}\n", encoding="utf-8")
        log("全部资源就绪。下一步：& .venv\\Scripts\\python.exe run_backend.py")
    else:
        (HERE / ".bootstrap_fail").write_text("部分资源缺失，见上方 MISS\n", encoding="utf-8")
        log("部分资源缺失（见上方 MISS）。补齐后重跑本脚本或单独补某步（--skip-*）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
