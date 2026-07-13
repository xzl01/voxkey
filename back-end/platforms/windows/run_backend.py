#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
r"""一键搭建并运行 VoxKey Windows 本地双引擎 ASR 后端（Qwen3-ASR Vulkan + FunASR DirectML）。

本脚本帮你完成：
  1. 在脚本所在目录创建/复用 .venv 虚拟环境；
  2. 安装运行依赖（torch 走 CPU 源，避免拉取数 GB 的 CUDA 版）；
  3. 校验模型与 DLL 资产是否就绪，并打印清单；
  4. 启动本地 FastAPI 服务（:17863），可选连带启动语音守护进程。

用法（用全路径 python 运行，绕开 uv / 受限卷的 trampoline 问题）：
  & "C:\Users\xzl01\AppData\Local\Programs\Python\Python312\python.exe" run_backend.py
  & "C:\...\Python312\python.exe" run_backend.py --skip-funasr   # 只跑 Qwen3（不下载 torch/funasr）
  & "C:\...\Python312\python.exe" run_backend.py --no-setup       # 假设依赖已装，直接启动
  & "C:\...\Python312\python.exe" run_backend.py --daemon        # 同时启动守护进程（热键需管理员）
  & "C:\...\Python312\python.exe" run_backend.py --port 17863    # 指定端口
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
CONFIG = HERE / "config.json"
CONFIG_EXAMPLE = HERE / "config.example.json"

# 服务默认端口（与 config.json / 守护进程一致）
DEFAULT_PORT = 17863

# 仅安装“服务必需 + 体量适中”的依赖；torch/funasr 视 --skip-funasr 决定。
BASE_DEPS = [
    "numpy>=1.26",
    "soundfile>=0.12",
    "fastapi>=0.110",
    "uvicorn>=0.29",
    "scipy>=1.11",
    "onnxruntime-directml>=1.18",
]


def log(msg: str) -> None:
    print(f"[run_backend] {msg}", flush=True)


def run(cmd, **kw) -> int:
    """Run a command, inheriting the console; returns exit code."""
    log("> " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, **kw).returncode


def pip_install(venv_py: Path, packages: list[str], extra_args: list[str] | None = None) -> int:
    """pip install with a couple of retries (network resets are common here)."""
    cmd = [str(venv_py), "-m", "pip", "install", "--no-input"] + (extra_args or []) + packages
    for attempt in range(1, 4):
        rc = subprocess.run(cmd).returncode
        if rc == 0:
            return 0
        log(f"pip install 失败（退出码 {rc}），第 {attempt}/3 次重试…")
        time.sleep(2)
    return rc


def ensure_venv() -> Path:
    if VENV.is_dir():
        log(f"复用已有虚拟环境：{VENV}")
    else:
        log("创建虚拟环境 .venv …")
        if subprocess.run([sys.executable, "-m", "venv", str(VENV)]).returncode != 0:
            raise SystemExit("venv 创建失败")
    return VENV / "Scripts" / "python.exe"


def setup(venv_py: Path, skip_funasr: bool) -> bool:
    """安装依赖。返回 funasr 是否成功安装（失败时不中止，便于 Qwen3 单引擎先跑）。"""
    log("安装依赖（torch 将使用 CPU 源）…")
    # 先装 CPU 版 torch，避免后续 requirements 触发 CUDA 巨包。
    if not skip_funasr:
        if pip_install(venv_py, ["torch"], extra_args=["--index-url", "https://download.pytorch.org/whl/cpu"]) != 0:
            raise SystemExit("torch(CPU) 安装失败")
    if pip_install(venv_py, BASE_DEPS) != 0:
        raise SystemExit("基础依赖安装失败")
    if skip_funasr:
        return False
    if pip_install(venv_py, ["funasr>=1.1"]) != 0:
        log("警告：funasr 安装失败（可能是网络重置或体积较大）。将仅启用 Qwen3 引擎。")
        log("      稍后可单独运行：.venv\\Scripts\\python.exe -m pip install funasr")
        return False
    return True


def ensure_config() -> None:
    if not CONFIG.is_file():
        if CONFIG_EXAMPLE.is_file():
            shutil.copy(CONFIG_EXAMPLE, CONFIG)
            log(f"已从示例生成 {CONFIG.name}（请按需填写模型路径）")
        else:
            raise SystemExit("config.json 与 config.example.json 均缺失")
        return
    # 已存在旧 config：若缺少 engines/fusion 段（旧 schema），用示例补齐，
    # 避免 load_engines 因缺字段而 KeyError。保留用户已有的其它设置。
    if CONFIG_EXAMPLE.is_file():
        try:
            cur = json.loads(CONFIG.read_text(encoding="utf-8"))
            ex = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
            changed = False
            for key in ("engines", "fusion"):
                if key not in cur and key in ex:
                    cur[key] = ex[key]
                    changed = True
            if changed:
                CONFIG.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                log("已用示例补齐 config.json 缺失的 engines/fusion 段")
        except Exception as exc:  # noqa: BLE001
            log(f"config.json 合并检查跳过（不影响启动）：{exc}")


def load_cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"无法读取 {CONFIG.name}: {exc}")


def check_assets(cfg: dict) -> None:
    """Best-effort 资产清单：缺失只警告，不致命（缺失的引擎会在 /health 中缺席）。"""
    log("资产校验：")
    base = HERE
    asr_project = Path(cfg.get("asr_project_dir", "qwen-asr"))
    if not asr_project.is_absolute():
        asr_project = (base / asr_project).resolve()
    engines = cfg.get("engines", {})

    # Qwen3 资产
    q = engines.get("qwen3_gpu", {})
    if q.get("enabled", True):
        mdir = Path(q.get("model_dir", "models/qwen3_asr"))
        if not mdir.is_absolute():
            mdir = (base / mdir).resolve()
        llm = mdir / q.get("llm_fn", "qwen3_asr_llm.q4_k_m.gguf")
        enc_f = mdir / q.get("encoder_frontend_fn", "qwen3_asr_encoder_frontend.int4.onnx")
        enc_b = mdir / q.get("encoder_backend_fn", "qwen3_asr_encoder_backend.int4.onnx")
        bin_dir = asr_project / "qwen_asr_gguf" / "inference" / "bin"
        dll_vk = bin_dir / "ggml-vulkan.dll"
        dll_llama = bin_dir / "llama.dll"
        log(f"  [Qwen3] GGUF      : {'OK ' if llm.is_file() else 'MISSING'} {llm}")
        log(f"  [Qwen3] enc-front : {'OK ' if enc_f.is_file() else 'MISSING'} {enc_f}")
        log(f"  [Qwen3] enc-back  : {'OK ' if enc_b.is_file() else 'MISSING'} {enc_b}")
        log(f"  [Qwen3] Vulkan DLL: {'OK ' if dll_vk.is_file() else 'MISSING'} {dll_vk}")
        log(f"  [Qwen3] llama DLL : {'OK ' if dll_llama.is_file() else 'MISSING'} {dll_llama}")
        if not (asr_project / "qwen_asr_gguf").is_dir():
            log(f"  [Qwen3] asr_project_dir 未指向含 qwen_asr_gguf 的项目：{asr_project}")

    # FunASR 资产
    f = engines.get("funasr_directml", {})
    if f.get("enabled", True):
        mpath = Path(f.get("model_path", "models/funasr_directml"))
        if not mpath.is_absolute():
            mpath = (base / mpath).resolve()
        onnx = mpath / "model.onnx"
        tok = mpath / "tokens.txt"
        mvn = mpath / "am.mvn"
        log(f"  [FunASR] model.onnx: {'OK ' if onnx.is_file() else 'MISSING'} {onnx}")
        log(f"  [FunASR] tokens.txt : {'OK ' if tok.is_file() else 'MISSING'} {tok}")
        log(f"  [FunASR] am.mvn     : {'OK ' if mvn.is_file() else 'MISSING'} {mvn}")
    log("（MISSING 仅代表对应引擎不会加载，服务仍可启动；放好资产后重启即可）")


def _port_listening(port: int) -> bool:
    """Best-effort check whether the local ASR service is already serving."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _tail_files(prefix: str, paths: list[Path], stop_event: threading.Event) -> None:
    """从日志文件尾部读取并镜像到控制台。文件写入端不会因缓冲上限阻塞，
    所以这里的阻塞读取对子进程无副作用。线程随 stop_event 结束（主循环
    在 Ctrl+C 时置位），不必自行探测进程退出。"""
    files = []
    for p in paths:
        try:
            files.append(open(p, "rb"))
        except Exception:
            files.append(None)
    try:
        while not stop_event.is_set():
            any_data = False
            for f in files:
                if f is None:
                    continue
                try:
                    raw = f.readline()
                except Exception:
                    continue
                if not raw:
                    continue
                any_data = True
                line = raw.decode("utf-8", errors="replace").rstrip()
                print(f"{prefix} {line}", flush=True)
            if not any_data:
                time.sleep(0.2)
    except Exception:
        pass
    finally:
        for f in files:
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass


def launch(venv_py: Path, port: int, with_daemon: bool) -> int:
    # NOTE: .venv/Scripts/python.exe is a launcher shim that re-execs the base
    # interpreter while keeping sys.prefix pointed at the venv (so venv site
    # packages like numpy/onnxruntime load). Always spawn children via venv_py.
    # 子进程输出必须重定向到【文件】而非管道：llama.cpp 加载进度条用回车（\r）
    # 刷新、不带换行，会把 64KB 管道填满，导致子进程阻塞在写、服务永远 bind
    # 不上。文件写入端不受此缓冲上限限制，再用 _tail_files 安全镜像到控制台。
    env = os.environ.copy()
    env.setdefault("GGML_VK_DISABLE_F16", "1")  # Radeon 780M 稳妥起见先开；想提速可设 '0'
    env.setdefault("OMP_NUM_THREADS", "4")       # 共享内存 APU 限线程，避免与系统争用
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["VOXKEY_ASR_PORT"] = str(port)

    procs = []
    tail_threads = []
    stop_evt = threading.Event()

    def spawn(name: str, script: Path, logfile: Path) -> None:
        out = open(logfile, "wb")
        err = open(HERE / (logfile.stem + ".err.log"), "wb")
        p = subprocess.Popen(
            [str(venv_py), str(script)],
            cwd=str(HERE), env=env, stdout=out, stderr=err,
        )
        procs.append((name, p))
        t = threading.Thread(
            target=_tail_files,
            args=(f"[{name}]", [logfile, HERE / (logfile.stem + ".err.log")], stop_evt),
            daemon=True,
        )
        t.start()
        tail_threads.append(t)

    log(f"启动本地 ASR 服务（http://127.0.0.1:{port}）…")
    spawn("service", HERE / "service.py", HERE / "service.run.log")

    if with_daemon:
        log("启动语音守护进程（热键需管理员权限；否则以普通权限运行将无热键）…")
        spawn("daemon", HERE / "windows_daemon.py", HERE / "daemon.run.log")

    log("已启动。按 Ctrl+C 停止全部进程。")
    exited_logged = set()
    try:
        while True:
            time.sleep(0.5)
            for name, p in procs:
                rc = p.poll()
                if rc is not None and name not in exited_logged:
                    exited_logged.add(name)
                    # .venv/Scripts/python.exe is a launcher shim that re-execs
                    # the base interpreter; the tracked shim may exit while the
                    # real service keeps running. Only treat it as fatal if the
                    # port is not actually serving.
                    if name == "service" and _port_listening(port):
                        log("service 子进程（launcher shim）已退出，但服务端口仍在监听，忽略。")
                    else:
                        log(f"{name} 已退出（退出码 {rc}）")
    except KeyboardInterrupt:
        log("收到中断，正在停止子进程…")
        stop_evt.set()
        # .venv/Scripts/python.exe 是 launcher shim，会 re-exec 出系统解释器，
        # 真正的 uvicorn 是孙子进程。仅 terminate 直接子进程会留下孤儿 server，
        # 所以按命令行特征补杀本脚本树下的 service.py / windows_daemon.py。
        import csv
        import io

        targets = {p.pid for _name, p in procs if p.pid}
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                 "| Select-Object ProcessId,CommandLine | ConvertTo-Csv -NoTypeInformation"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            ).stdout
            for row in csv.reader(io.StringIO(out)):
                if len(row) < 2:
                    continue
                try:
                    pid = int(row[0])
                except ValueError:
                    continue
                cl = row[1] if len(row) > 1 else ""
                if any(k in cl for k in ("service.py", "windows_daemon.py", "run_backend.py")):
                    targets.add(pid)
        except Exception:
            pass
        for pid in targets:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            except Exception:
                pass
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="VoxKey Windows 本地双引擎 ASR 启动器")
    ap.add_argument("--no-setup", action="store_true", help="跳过依赖安装，直接启动")
    ap.add_argument("--skip-funasr", action="store_true", help="不安装/不加载 FunASR（仅 Qwen3，省去 torch/funasr）")
    ap.add_argument("--daemon", action="store_true", help="同时启动语音守护进程")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="ASR 服务端口")
    args = ap.parse_args()

    ensure_config()
    cfg = load_cfg()
    check_assets(cfg)

    venv_py = ensure_venv()
    funasr_ok = True
    if not args.no_setup:
        funasr_ok = setup(venv_py, args.skip_funasr)

    # 若显式跳过、或 funasr 实际未装上，临时在运行期关闭该引擎（不改 config.json），
    # 避免加载 torch/funasr 导致整个服务起不来；Qwen3 仍可独立运行。
    if args.skip_funasr or not funasr_ok:
        cfg.setdefault("engines", {}).setdefault("funasr_directml", {})["enabled"] = False
        CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if not funasr_ok and not args.skip_funasr:
            log("已因 funasr 未安装而临时关闭 FunASR 引擎；Qwen3 仍可运行")
        else:
            log("已临时关闭 FunASR 引擎（--skip-funasr）")

    return launch(venv_py, args.port, args.daemon)


if __name__ == "__main__":
    sys.exit(main())
