"""Local trace visualizer server.

Serves the static frontend plus two JSON endpoints:
- GET /api/sessions      -> all discovered Claude Code + Codex sessions
- GET /api/session?id=.. -> one parsed, normalized session

Stdlib only; run with `python3 visualizer/server.py [--port N]`.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import trace_parsers

STATIC_DIR = Path(__file__).resolve().parent / "static"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)  # type: ignore[arg-type]

    def end_headers(self) -> None:
        # Traces and the frontend both change out from under the browser; never cache.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/sessions":
            try:
                self._send_json({"sessions": trace_parsers.list_sessions()})
            except (ValueError, OSError, KeyError, TypeError) as exc:
                # A bad trace must never kill the endpoint.
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif parsed.path == "/api/session":
            session_id = parse_qs(parsed.query).get("id", [""])[0]
            try:
                self._send_json(trace_parsers.load_session(session_id))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=400)
        else:
            super().do_GET()

    def _send_json(self, obj: object, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client hung up; nothing to do

    def log_message(self, format: str, *args: object) -> None:
        pass  # keep the terminal quiet


def main() -> None:
    parser = argparse.ArgumentParser(description="Local trace visualizer for Claude Code and Codex")
    parser.add_argument("--port", type=int, default=8484)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Trace visualizer: http://{args.host}:{args.port}")
    print(f"  Claude Code traces: {trace_parsers.CLAUDE_ROOT}")
    print(f"  Codex traces:       {trace_parsers.CODEX_ROOT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
