# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Functional verification for the NCE (FunASR/SenseVoice -> ORT-CoreML) engine.

Runs the engine on a small, fixed AISHELL-1 subset under test/data/aishell1_zh
and asserts it transcribes real audio with an acceptable character error rate.

The subset is generated (not the full corpus) for fast, repeatable checks:

    .venv/bin/python _build_eval_set.py --source aishell1 --split test \
        --data-dir /Volumes/拓展盘/Dev/data_aishell --limit 50 \
        --out test/data/aishell1_zh

Run with:
    .venv/bin/python -m pytest test/test_nce.py -q -s
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # back-end/platforms/macos
sys.path.insert(0, str(ROOT))

from _nce_eval import load_waveform, score  # noqa: E402
from funasr_coreml import FunASRCoreML  # noqa: E402

TEST_DIR = HERE / "data" / "aishell1_zh"
MODEL_DIR = ROOT / "models" / "funasr_coreml"
CER_THRESHOLD = 0.20  # functional gate; gross failures push CER near 1.0


def _pairs() -> list[str]:
    return sorted({p.stem for p in TEST_DIR.glob("*.wav")})


@pytest.fixture(scope="module")
def engine():
    compute = os.environ.get("NCE_COMPUTE", "ane")
    last: Exception | None = None
    for cu in (compute, "cpu"):
        try:
            eng = FunASRCoreML(str(MODEL_DIR), compute_units=cu)
            eng.warmup()
            yield eng
            eng.shutdown()
            return
        except Exception as exc:  # ANE may be unavailable in CI
            last = exc
    raise last  # type: ignore[misc]


def test_test_dir_populated():
    pairs = _pairs()
    assert pairs, f"no .wav in {TEST_DIR}; generate the subset first"
    assert len(pairs) >= 10, "subset too small for a meaningful check"


def test_transcribes_nonempty(engine):
    for stem in _pairs()[:5]:
        wf = load_waveform(TEST_DIR / f"{stem}.wav")
        hyp = engine.transcribe(wf, language="auto").text
        assert hyp.strip(), f"empty hypothesis for {stem}"


def test_cer_below_threshold(engine):
    agg = denom = 0
    for stem in _pairs():
        ref = (TEST_DIR / f"{stem}.txt").read_text(encoding="utf-8").strip()
        wf = load_waveform(TEST_DIR / f"{stem}.wav")
        hyp = engine.transcribe(wf, language="auto").text
        _, dist, _, rn, _ = score(ref, hyp)
        agg += dist
        denom += rn
    cer = agg / max(1, denom)
    print(f"\nNCE CER over {len(_pairs())} utts = {cer * 100:.2f}%")
    assert cer < CER_THRESHOLD, f"NCE CER too high: {cer * 100:.2f}%"
