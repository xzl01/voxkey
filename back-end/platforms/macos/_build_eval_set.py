# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Build a labeled test set for the NCE engine.

Sources:
  fleurs    google/fleurs  (zh-CN=cmn_hans_cn, en-US=en_us, ...)  -> transcription + audio
  aishell1  standard Mandarin, CER. Two input modes:
              * local:  --data-dir <OpenSLR data_aishell>
                        expects wav/<spk>.tar.gz (each holds train|dev|test/<spk>/*.wav)
                        + transcript/aishell_transcript_v0.8.txt
              * HF:     AISHELL/AISHELL-1  (needs HF_TOKEN, downloads the split)

NOTE: xuyaya/ASR-Testset is only a *curated README index*; we use aishell1-test.

Writes, per utterance:
    <out>/<id>.wav   16 kHz mono PCM16
    <out>/<id>.txt   reference transcript
"""

from __future__ import annotations

import argparse
import logging
import os
import tarfile
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxkey.buildset")


def _save(out: Path, uid: str, arr: np.ndarray, sr: int, ref: str) -> None:
    if sr != 16000:
        import torch
        import torchaudio.functional as F  # type: ignore

        arr = F.resample(torch.from_numpy(arr), sr, 16000).numpy()
    sf.write(str(out / f"{uid}.wav"), arr.astype(np.float32), 16000, format="WAV", subtype="PCM_16")
    (out / f"{uid}.txt").write_text(ref, encoding="utf-8")


def _from_fleurs(out: Path, lang: str, split: str, n: int, stream: bool) -> int:
    from datasets import load_dataset

    if stream:
        ds = load_dataset("google/fleurs", lang, split=split, streaming=True)
        it = ds.take(n) if n and n > 0 else ds
    else:
        # full, non-streaming download of the whole split
        ds = load_dataset("google/fleurs", lang, split=split, streaming=False)
        it = ds
    kept = 0
    for row in it:
        if stream and n and kept >= n:
            break
        audio = row.get("audio")
        if audio is None:
            continue
        arr = np.asarray(audio["array"], dtype=np.float32)
        sr = int(audio.get("sampling_rate", 16000))
        ref = (row.get("transcription") or row.get("raw_transcription") or "").strip()
        if not ref:
            continue
        uid = row.get("id") or f"{lang}-{kept:04d}"
        _save(out, uid, arr, sr, ref)
        kept += 1
        logger.info("  %d  %s  | %s", kept, uid, ref[:50])
    return kept


def _from_aishell1(out: Path, split: str, n: int, stream: bool) -> int:
    from datasets import load_dataset

    # AISHELL/AISHELL-1 has train/dev/test splits (audio/text/speaker/id).
    if stream:
        ds = load_dataset("AISHELL/AISHELL-1", split=split, streaming=True)
        it = ds.take(n) if n and n > 0 else ds
    else:
        # full, non-streaming download of the whole split
        ds = load_dataset("AISHELL/AISHELL-1", split=split, streaming=False)
        it = ds
    kept = 0
    for row in it:
        if stream and n and kept >= n:
            break
        audio = row.get("audio")
        if audio is None:
            continue
        arr = np.asarray(audio["array"], dtype=np.float32)
        sr = int(audio.get("sampling_rate", 16000))
        # aishell1 reference is in the `text` field
        ref = (row.get("text") or row.get("sentence") or "").strip()
        if not ref:
            continue
        uid = row.get("id") or row.get("utt_id") or f"aishell1_{kept:04d}"
        _save(out, uid, arr, sr, ref)
        kept += 1
        logger.info("  %d  %s  | %s", kept, uid, ref[:50])
    return kept


def _from_aishell1_local(data_dir: Path, out: Path, split: str, limit: int = 0) -> int:
    """Build a (partial) test set from a local OpenSLR `data_aishell` checkout.

    Each wav/<spk>.tar.gz holds members under train|dev|test/<spk>/*.wav, so the
    split is encoded in the path prefix; we keep only the <split>/ members and
    match them to transcript/aishell_transcript_v0.8.txt by utterance id.

    With `limit` > 0 we keep a deterministic, evenly-strided subset (spanning
    all test speakers) instead of the full split — for quick functional
    verification, not benchmarking.
    """
    trans_dir = data_dir / "transcript"
    cands = sorted(trans_dir.glob("*.txt"))
    if not cands:
        raise FileNotFoundError(f"no transcript .txt under {trans_dir}")
    trans = cands[0]
    utt2text: dict[str, str] = {}
    for line in trans.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        uid, _, txt = line.partition(" ")
        utt2text[uid] = txt.strip()

    wav_root = data_dir / "wav"
    # Pass 1: collect every qualifying member descriptor (tarball + member name).
    members: list[tuple[Path, str, str]] = []  # (tar_path, member_name, uid)
    for tar_path in sorted(wav_root.glob("*.tar.gz")):
        # Quick reject: every member in a tarball shares one split prefix.
        with tarfile.open(tar_path, "r:gz") as tar:
            first = tar.next()
            if first is None or first.name.split("/")[0] != split:
                continue
        with tarfile.open(tar_path, "r:gz") as tar:
            for m in tar.getmembers():
                parts = m.name.split("/")
                if len(parts) < 3 or parts[0] != split or not m.isfile():
                    continue
                if not m.name.endswith(".wav"):
                    continue
                uid = Path(m.name).stem
                if not utt2text.get(uid):
                    continue
                members.append((tar_path, m.name, uid))

    # Deterministic strided subset so the partial set spans all speakers.
    if limit and limit < len(members):
        step = len(members) // limit
        chosen = members[::step][:limit]
    else:
        chosen = members

    # Group by tarball so each archive is opened only once.
    by_tar: dict[Path, list[tuple[str, str]]] = {}
    for tp, name, uid in chosen:
        by_tar.setdefault(tp, []).append((name, uid))

    kept = 0
    for tar_path, items in by_tar.items():
        want = {n for n, _ in items}
        uid_of = {n: u for n, u in items}
        with tarfile.open(tar_path, "r:gz") as tar:
            for m in tar.getmembers():
                if m.name not in want:
                    continue
                uid = uid_of[m.name]
                ref = utt2text[uid]
                try:
                    fobj = tar.extractfile(m)
                    assert fobj is not None
                    raw = fobj.read()
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                        tf.write(raw)
                        tmpname = tf.name
                    try:
                        data, sr = sf.read(tmpname, dtype="float32", always_2d=False)
                    finally:
                        os.unlink(tmpname)
                    _save(out, uid, data, int(sr), ref)
                except Exception as exc:  # skip one bad clip, keep going
                    logger.warning("skip %s: %s", uid, exc)
                    continue
                kept += 1
                if kept % 250 == 0:
                    logger.info("  %d  %s", kept, uid)
    return kept


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["fleurs", "aishell1"], default="aishell1")
    ap.add_argument("--lang", default="cmn_hans_cn", help="fleurs config, e.g. cmn_hans_cn / en_us")
    ap.add_argument("--split", default="test")
    ap.add_argument("--n", type=int, default=0, help="sample size when streaming; 0 = use --full")
    ap.add_argument(
        "--full", action="store_true", help="download the ENTIRE split (non-streaming), ignore --n"
    )
    ap.add_argument(
        "--data-dir",
        default="",
        help="local OpenSLR data_aishell dir; with --source aishell1 "
        "this uses the local wav/*.tar.gz + transcript instead of HF",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="local aishell1 only: keep a strided subset of N "
        "utterances for functional verification; 0 = full split",
    )
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stream = not args.full

    if args.source == "fleurs":
        kept = _from_fleurs(out, args.lang, args.split, args.n, stream)
    elif args.source == "aishell1" and args.data_dir:
        kept = _from_aishell1_local(Path(args.data_dir), out, args.split, args.limit)
    else:
        kept = _from_aishell1(out, args.split, args.n, stream)

    logger.info("Wrote %d utterances to %s", kept, out)
    if kept == 0:
        logger.error(
            "Wrote 0 utterances — verify the dataset's audio/text field "
            "names match the source branch in _build_eval_set.py "
            "(AISHELL/AISHELL-1 uses 'audio' + 'text')."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
