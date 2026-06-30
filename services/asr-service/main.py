#!/usr/bin/env python3
"""Minimal local ASR service placeholder.

This keeps the desktop UI independent from model runtimes while the real
Qwen3-ASR backend is being ported into VoxKey.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps({"ok": True, "service": "voxkey-asr"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
