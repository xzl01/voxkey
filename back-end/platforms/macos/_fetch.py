# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT
"""Shared download helpers with mirror fallback for VoxKey model assets.

Endpoints are tried in order: ``base_url`` first, then each entry of
``mirrors``. This lets a primary source (e.g. HuggingFace) fail over to a
China-accessible mirror (e.g. hf-mirror.com) automatically.

Usage:
    from _fetch import fetch_one, fetch_archive
"""

from __future__ import annotations

import hashlib
import shutil
import socket
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def _iter_urls(base_url: str, path: str, mirrors: list[str]) -> list[str]:
    """Build the candidate URLs for ``path``.

    Accepts either a *base directory* (the file name is appended) or a *full
    archive URL* already ending in ``path`` (used as-is). This lets
    ``VOXKEY_FUNASR_URLS`` point at either a release folder or the complete
    ``.tar.gz`` URL, matching the README examples.
    """
    norm = path.lstrip("/")

    def _join(base: str) -> str:
        b = base.rstrip("/")
        if b == norm or b.endswith("/" + norm):
            return b  # base already includes the file name
        return b + "/" + norm

    urls: list[str] = []
    if base_url:
        urls.append(_join(base_url))
    for m in mirrors or []:
        if m:
            urls.append(_join(m))
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _download(url: str, dst: Path, *, timeout: int = 600, sha256: str | None = None) -> None:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"  \u2193 {url}")
    socket.setdefaulttimeout(timeout)
    tmp = dst.with_suffix(dst.suffix + ".part")
    try:
        urllib.request.urlretrieve(url, tmp)
    except Exception:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise
    if sha256:
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest().lower() != sha256.lower():
            tmp.unlink(missing_ok=True)
            raise ValueError(f"SHA256 mismatch for {url}")
    shutil.move(str(tmp), str(dst))


def _safe_extract(archive: Path, out_dir: Path) -> None:
    """Extract a tar.gz archive into ``out_dir`` while rejecting any member that
    escapes the target (absolute paths, ``..`` traversal, or sym/hard links
    pointing outside it). This prevents a malicious or tampered archive from
    writing outside the intended model directory.
    """
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            dest = (out_dir / member.name).resolve()
            # Reject traversal / absolute paths.
            if dest != out_dir and out_dir not in dest.parents:
                raise ValueError(f"archive member escapes target: {member.name}")
            # Reject links that point outside the target.
            if member.issym() or member.islnk():
                target = member.linkname
                link_dest = (dest.parent / target).resolve()
                if link_dest != out_dir and out_dir not in link_dest.parents:
                    raise ValueError(f"archive link escapes target: {member.name}")
        # Second pass: actually extract (paths validated above).
        tf.extractall(out_dir)


def fetch_one(
    path: str,
    dst: Path,
    *,
    base_url: str = "",
    mirrors: list[str] | None = None,
    sha256: str | None = None,
    timeout: int = 600,
) -> bool:
    """Download ``path`` (relative to base_url/mirrors) into ``dst``.

    Tries each source in order and returns True on first success. Raises the
    last error if every source fails. If ``sha256`` is given, the downloaded
    bytes are verified against it before being moved into place, so a tampered
    mirror or release asset is rejected instead of used.
    """
    urls = _iter_urls(base_url, path, mirrors or [])
    if not urls:
        raise RuntimeError("No base_url or mirrors provided")
    last: Exception | None = None
    for u in urls:
        try:
            _download(u, Path(dst), timeout=timeout, sha256=sha256)
            return True
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"  ! mirror failed: {u} ({exc}); trying next")
    assert last is not None
    raise last


def fetch_archive(
    path: str,
    out_dir: Path,
    *,
    base_url: str = "",
    mirrors: list[str] | None = None,
    sha256: str | None = None,
    timeout: int = 900,
) -> None:
    """Download a tar.gz archive and extract its contents safely into ``out_dir``.

    If ``sha256`` is supplied the archive is verified before extraction.
    Extraction rejects any member attempting to escape the target directory.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / "asset.tar.gz"
        fetch_one(path, archive, base_url=base_url, mirrors=mirrors, sha256=sha256, timeout=timeout)
        print(f"  \u21e9 extracting -> {out_dir}")
        _safe_extract(archive, out_dir)
