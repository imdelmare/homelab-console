from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from urllib.parse import unquote

from .store import SentinelStore


class HeartbeatServer:
    def __init__(self, host: str, port: int, token: str, store: SentinelStore) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.store = store
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    def start(self) -> None:
        token = self.token
        store = self.store

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/health":
                    self.send_error(404)
                    return
                self._json(200, {"ok": True})

            def do_POST(self) -> None:  # noqa: N802
                prefix = "/heartbeat/"
                if not self.path.startswith(prefix):
                    self.send_error(404)
                    return
                if not token or not _authorized(self.headers, token):
                    self._json(401, {"ok": False, "error": "unauthorized"})
                    return
                source_id = unquote(self.path[len(prefix) :]).strip()
                if not source_id:
                    self._json(400, {"ok": False, "error": "missing_source_id"})
                    return
                payload = _read_json(self)
                remote_addr = self.client_address[0] if self.client_address else ""
                store.record_heartbeat(source_id, remote_addr, payload)
                self._json(200, {"ok": True})

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _authorized(headers: Any, token: str) -> bool:
    bearer = headers.get("Authorization", "")
    header_token = headers.get("X-Sentinel-Token", "")
    return secrets.compare_digest(bearer, f"Bearer {token}") or secrets.compare_digest(
        header_token, token
    )


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = min(int(handler.headers.get("Content-Length", "0") or "0"), 4096)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
