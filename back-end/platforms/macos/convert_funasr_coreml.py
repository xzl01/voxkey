# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Export a FunASR SenseVoice model to ONNX for the NCE (Apple Neural Engine).

The NCE engine (funasr_coreml.FunASRCoreML) runs the ONNX graph directly
through **ONNX Runtime's Core ML Execution Provider**, which dispatches
supported ops onto the ANE and falls the rest back to CPU. We do NOT convert
to a ``.mlpackage`` — coremltools >= 8 dropped its ONNX frontend, and
ORT-CoreML is the maintained, robust way to run ONNX on the ANE.

This script:
  1. Loads the FunASR model (e.g. iic/SenseVoiceSmall) and exports its
     encoder to ONNX via torch.onnx.export (legacy TorchScript exporter, to
     avoid the torch>=2.13 dynamo/version_converter "Pad" failure).
  2. Optionally dynamic-quantizes the ONNX to int8 with ONNX Runtime.
  3. Copies the WavFrontend assets the engine needs:
       - am.mvn            (CMVN mean/std for the 560-dim LFR features)
       - tokens.txt         (SentencePiece pieces, one per line, in vocab order)
       - frontend.json      (frontend_conf + lid/textnorm maps + defaults)

SenseVoice ONNX contract (what the engine feeds / reads):
  inputs : speech[B, T, 560] f32, speech_lengths[B] i32,
           language[B] i32 (0=auto,3=zh,4=en,7=yue,11=ja,12=ko,13=nospeech),
           textnorm[B] i32 (14=withitn, 15=woitn)
  outputs: ctc_logits[B, T', 25055] f32, encoder_out_lens[B] i32

Usage:
  python convert_funasr_coreml.py --model iic/SenseVoiceSmall \
      --out models/funasr_coreml --quantize int8

Requires: funasr, modelscope, torch, torchaudio, onnx, onnxruntime,
sentencepiece.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
from pathlib import Path


def logger() -> logging.Logger:
    return logging.getLogger("voxkey.convert")


# Common short names -> FunASR/ModelScope model ids
_MODEL_ALIASES = {
    "sensevoice_small": "iic/SenseVoiceSmall",
    "sensevoice": "iic/SenseVoiceSmall",
}


def _copy_frontend_assets(model, out_dir: Path) -> None:
    """Copy am.mvn, build tokens.txt, and write frontend.json for the engine."""
    kw = model.kwargs
    fe_conf = dict(kw.get("frontend_conf", {}) or {})
    tok_conf = dict(kw.get("tokenizer_conf", {}) or {})

    # CMVN
    cmvn_src = Path(fe_conf.get("cmvn_file", ""))
    if cmvn_src.is_file():
        shutil.copyfile(cmvn_src, out_dir / "am.mvn")
        logger().info("Copied am.mvn <- %s", cmvn_src.name)
    else:
        logger().warning("cmvn_file not found: %s", cmvn_src)

    # SentencePiece vocab -> tokens.txt (index == sp piece id == ctc_logits axis)
    sp_model = Path(tok_conf.get("bpemodel", ""))
    if sp_model.is_file():
        import sentencepiece as sp

        proc = sp.SentencePieceProcessor()
        proc.Load(str(sp_model))
        n = proc.GetPieceSize()
        with open(out_dir / "tokens.txt", "w", encoding="utf-8") as f:
            for i in range(n):
                f.write(proc.IdToPiece(i) + "\n")
        logger().info("Wrote tokens.txt with %d pieces (sp model: %s)", n, sp_model.name)
    else:
        logger().warning("SentencePiece model not found: %s", sp_model)

    # frontend.json: everything the engine needs to rebuild WavFrontend + maps
    fe_conf.pop("cmvn_file", None)  # engine points at local am.mvn
    payload = {
        "frontend_conf": fe_conf,
        "cmvn_file": "am.mvn",
        "lid_dict": getattr(model.model, "lid_dict", {}),
        "textnorm_dict": getattr(model.model, "textnorm_dict", {}),
        "default_language": 0,  # auto
        "default_textnorm": 15,  # woitn (no inverse text normalization)
        "vocab_size": getattr(model.model, "vocab_size", None),
    }
    (out_dir / "frontend.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger().info("Wrote frontend.json")


def export_onnx(model_name: str, out_dir: Path):
    import torch
    from funasr import AutoModel

    # Force the legacy (TorchScript) ONNX exporter. torch>=2.13's default
    # dynamo exporter emits very new opset ops, then fails in
    # onnx.version_converter ("No Adapter To Version $17 for Pad"). The legacy
    # exporter produces a clean opset-17 graph that ORT-CoreML handles fine.
    _orig = torch.onnx.export

    def _legacy(*a, **k):
        k.setdefault("dynamo", False)
        return _orig(*a, **k)

    torch.onnx.export = _legacy

    model_id = _MODEL_ALIASES.get(model_name.lower(), model_name)
    logger().info("Loading FunASR model '%s' (id=%s) ...", model_name, model_id)
    model = AutoModel(model=model_id, disable_update=True)

    # AutoModel.export() runs torch.onnx.export internally and returns the
    # export *directory* containing model.onnx (+ tokens/config when available).
    export_dir = Path(model.export(type="onnx", quantize=False, opset_version=17, device="cpu"))
    onnx_path = export_dir / "model.onnx"
    if not onnx_path.is_file():
        cands = sorted(export_dir.glob("*.onnx"))
        if not cands:
            raise RuntimeError(f"FunASR export produced no .onnx in {export_dir}")
        onnx_path = cands[0]

    _copy_frontend_assets(model, out_dir)
    return onnx_path


def place_onnx(onnx_path: Path, out_dir: Path, quantize: str) -> Path:
    import onnx
    import onnxruntime as ort
    from onnxruntime.quantization import quantize_dynamic, QuantType

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "model.onnx"

    if quantize in ("int8", "int4"):
        tmp = out_dir / "_qtmp.onnx"
        quantize_dynamic(str(onnx_path), str(tmp), weight_type=QuantType.QInt8)
        validated = onnx.load(str(tmp))
        onnx.checker.check_model(validated)
        onnx.save(validated, str(target))
        tmp.unlink(missing_ok=True)
        logger().info("Saved int8 ONNX -> %s", target)
    elif quantize == "fp16":
        try:
            from onnxruntime.transformers import float16

            m = float16.convert_float_to_float16(onnx.load(str(onnx_path)))
            onnx.save(m, str(target))
            logger().info("Saved fp16 ONNX -> %s", target)
        except Exception as exc:  # pragma: no cover - optional dep
            logger().warning("fp16 conversion unavailable (%s); copying float32", exc)
            shutil.copyfile(onnx_path, target)
    else:
        shutil.copyfile(onnx_path, target)
        logger().info("Saved float32 ONNX -> %s", target)

    # sanity check it loads under ORT-CoreML
    providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    sess = ort.InferenceSession(str(target), providers=providers)
    logger().info(
        "ORT-CoreML providers=%s inputs=%s outputs=%s",
        sess.get_providers(),
        [i.name for i in sess.get_inputs()],
        [o.name for o in sess.get_outputs()],
    )
    return target


def main() -> int:
    logging.basicConfig(level=logging.INFO)

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="FunASR model id (e.g. iic/SenseVoiceSmall)")
    ap.add_argument("--onnx", help="existing ONNX path to place")
    ap.add_argument("--out", required=True)
    ap.add_argument("--quantize", choices=["none", "fp16", "int8", "int4"], default="int8")
    args = ap.parse_args()

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.onnx:
        onnx_path = Path(args.onnx).expanduser()
    elif args.model:
        onnx_path = export_onnx(args.model, out_dir)
    else:
        raise SystemExit("Provide --model or --onnx")
    place_onnx(onnx_path, out_dir, args.quantize)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
