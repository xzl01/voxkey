#!/usr/bin/env python3
"""Minimal local ASR service placeholder.

This keeps the desktop UI independent from model runtimes while the real
Qwen3-ASR backend is being ported into VoxKey.

Endpoints:
  GET  /health     -> {"ok": true, "service": "voxkey-asr"}
  POST /transcribe -> accepts raw audio bytes, persists them and returns a stub
                      transcription so the desktop UI can exercise the full HTTP
                      path before the model backend is wired in.
"""

from __future__ import annotations

import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPLOAD_DIR = os.environ.get("VOXKEY_ASR_UPLOAD_DIR", tempfile.gettempdir())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?")[0] != "/health":
            self.send_error(404)
            return
        self._send_json(200, {"ok": True, "service": "voxkey-asr"})

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/transcribe":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        data = self.rfile.read(length) if length > 0 else b""

        # The real Qwen3-ASR-GGUF backend is not wired into the service yet. We
        # still persist the audio for debugging, but respond 501 so callers (the
        # daemon / desktop UI) get an honest signal instead of a silently empty
        # transcription. The HTTP backend therefore requires asr_fallback_local
        # or a real backend to be useful.
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="voxkey-asr-", dir=UPLOAD_DIR)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)

        self._send_json(
            501,
            {
                "text": "",
                "status": "not_implemented",
                "received_bytes": len(data),
                "saved_to": path,
                "note": "Qwen3-ASR backend not wired yet; returning 501 Not Implemented.",
            },
        )

    def _send_json(self, code: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep default logging quiet; errors still surface via stderr through
        # the request handlers above.
        pass


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 17863), Handler)
    print("ASR service listening on http://127.0.0.1:17863")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nASR service stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
