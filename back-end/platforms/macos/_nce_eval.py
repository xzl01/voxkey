# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Evaluate the NCE (FunASR/SenseVoice -> ORT-CoreML) engine on a test set.

Expected layout of --test-dir:
    <utt_id>.wav    16 kHz mono audio (any format ffmpeg can read is fine)
    <utt_id>.txt    reference transcript (plain text, one line)

For each pair it runs FunASRCoreML and scores the hypothesis against the
reference with CER (character error rate, for CJK) and WER (token/word
error rate, whitespace-tokenized). Aggregated CER/WER + a JSON report are
printed.

Usage:
    python _nce_eval.py --test-dir models/funasr_eval_zh --compute ane
    python _nce_eval.py --test-dir models/funasr_eval_en --compute cpu \
        --report out/en_report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxkey.eval")


def load_waveform(path: Path) -> np.ndarray:
    """Decode any audio file to 16 kHz mono float32 via soundfile/ffmpeg."""
    import soundfile as sf

    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    except Exception:
        # fall back through ffmpeg if soundfile can't read the container
        import subprocess
        import tempfile

        tmp = Path(tempfile.mktemp(suffix=".wav"))
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-ar", "16000", "-ac", "1", "-f", "wav", str(tmp)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        data, sr = sf.read(str(tmp), dtype="float32", always_2d=False)
        tmp.unlink(missing_ok=True)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 16000:
        import torchaudio.functional as F  # type: ignore
        import torch

        data = F.resample(torch.from_numpy(data), sr, 16000).numpy()
    return data.astype(np.float32)


_CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")


def norm_text(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def edits(ref: list[str], hyp: list[str]) -> int:
    """Levenshtein distance (number of edits)."""
    n, m = len(ref), len(hyp)
    dp = list(range(m + 1))
    for i in range(1, n + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, m + 1):
            cur = dp[j]
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + cost)
            prev = cur
    return dp[m]


def score(ref: str, hyp: str):
    ref, hyp = norm_text(ref), norm_text(hyp)
    has_cjk = bool(_CJK.search(ref + hyp))
    if has_cjk:
        # CER: ignore whitespace so differing word-segmentation between the
        # reference and the hypothesis does not inflate the character error rate.
        r, h = list(re.sub(r"\s+", "", ref)), list(re.sub(r"\s+", "", hyp))
        kind = "cer"
    else:
        r = ref.split()
        h = hyp.split()  # word-level (WER)
        kind = "wer"
    dist = edits(r, h)
    denom = max(1, len(r))
    return kind, dist / denom, dist, len(r), len(h)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--test-dir",
        default="test/data/aishell1_zh",
        help="dir of <id>.wav + <id>.txt pairs (default: the partial AISHELL-1 subset under test/)",
    )
    ap.add_argument("--model-dir", default="models/funasr_coreml")
    ap.add_argument("--compute", choices=["ane", "cpu"], default="ane")
    ap.add_argument("--language", default="auto")
    ap.add_argument("--limit", type=int, default=0, help="eval only first N pairs")
    ap.add_argument("--report", default="", help="optional JSON report path")
    args = ap.parse_args()

    from funasr_coreml import FunASRCoreML

    eng = FunASRCoreML(str(Path(args.model_dir)), compute_units=args.compute)
    eng.warmup()

    root = Path(args.test_dir)
    pairs = sorted({p.stem for p in root.glob("*.wav")})
    if args.limit:
        pairs = pairs[: args.limit]
    logger.info("Evaluating %d utterances from %s (compute=%s)", len(pairs), root, args.compute)

    rows, agg = [], {"cer": [0, 0], "wer": [0, 0]}
    for stem in pairs:
        wav = root / f"{stem}.wav"
        txt = root / f"{stem}.txt"
        if not txt.is_file():
            logger.warning("skip %s: missing .txt", stem)
            continue
        ref = txt.read_text(encoding="utf-8").strip()
        try:
            wf = load_waveform(wav)
            t0 = time.perf_counter()
            tr = eng.transcribe(wf, language=args.language)
            dt = time.perf_counter() - t0
            hyp = tr.text
        except Exception as exc:
            logger.warning("FAIL %s: %s", stem, exc)
            rows.append({"id": stem, "ref": ref, "hyp": "", "err": repr(exc)})
            continue
        kind, rate, dist, rn, hn = score(ref, hyp)
        agg[kind][0] += dist
        agg[kind][1] += rn
        rows.append(
            {
                "id": stem,
                "ref": ref,
                "hyp": hyp,
                "kind": kind,
                "rate": round(rate, 4),
                "latency_s": round(dt, 3),
            }
        )
        logger.info("[%s] %s %.3f  %s", kind, stem, rate, hyp[:40])

    report = {
        "compute": args.compute,
        "n": len(rows),
        "cer": (agg["cer"][0] / agg["cer"][1]) if agg["cer"][1] else None,
        "wer": (agg["wer"][0] / agg["wer"][1]) if agg["wer"][1] else None,
        "rows": rows,
    }
    print("\n===== SUMMARY =====")
    if report["cer"] is not None:
        print(f"CER = {report['cer'] * 100:.2f}%  (over {agg['cer'][1]} chars)")
    if report["wer"] is not None:
        print(f"WER = {report['wer'] * 100:.2f}%  (over {agg['wer'][1]} words)")
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"report -> {args.report}")
    eng.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
