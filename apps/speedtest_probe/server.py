"""Narrow, on-demand Speedtest probe for the home network.

The separately licensed CLI is supplied by the operator at image-build time.
The service exposes one authenticated operation that runs one fixed command;
it has no generic command, argument, server-selection or scheduling surface.
"""

from __future__ import annotations

import hmac
import json
import os
import subprocess
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TIMEOUT_SECONDS = min(
    180, max(30, int(os.environ.get("SPEEDTEST_PROBE_TIMEOUT_SECONDS", "120")))
)
LISTEN_HOST = os.environ.get("SPEEDTEST_PROBE_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("SPEEDTEST_PROBE_LISTEN_PORT", "8780"))
BEARER_TOKEN = os.environ.get("SPEEDTEST_PROBE_TOKEN", "")

_run_lock = threading.Lock()


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} is not numeric")
    return float(value)


def normalize_speedtest(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("speedtest output is not an object")
    ping = payload.get("ping")
    download = payload.get("download")
    upload = payload.get("upload")
    server = payload.get("server")
    isp = payload.get("isp")
    interface = payload.get("interface")
    if not all(isinstance(item, dict) for item in (ping, download, upload, server, interface)):
        raise ValueError("speedtest output is missing required objects")
    if not isinstance(isp, str):
        raise ValueError("speedtest output is missing ISP")

    result = payload.get("result")
    result_url = result.get("url") if isinstance(result, dict) else None
    packet_loss = payload.get("packetLoss")
    return {
        "measured_at": str(payload.get("timestamp") or datetime.now(UTC).isoformat()),
        "download_mbps": round(_number(download.get("bandwidth"), "download.bandwidth") * 8 / 1_000_000, 2),
        "upload_mbps": round(_number(upload.get("bandwidth"), "upload.bandwidth") * 8 / 1_000_000, 2),
        "ping_ms": round(_number(ping.get("latency"), "ping.latency"), 2),
        "jitter_ms": round(_number(ping.get("jitter"), "ping.jitter"), 2),
        "packet_loss_percent": (
            round(_number(packet_loss, "packetLoss"), 2) if packet_loss is not None else None
        ),
        "server": {
            "id": int(_number(server.get("id"), "server.id")),
            "name": str(server.get("name") or ""),
            "location": str(server.get("location") or ""),
            "country": str(server.get("country") or ""),
        },
        "isp": isp,
        "interface_name": str(interface.get("name") or ""),
        "result_url": str(result_url) if result_url else None,
    }


def run_speedtest() -> dict[str, Any]:
    completed = subprocess.run(
        [
            "/opt/speedtest-cli/speedtest",
            "--accept-license",
            "--accept-gdpr",
            "--format=json",
            "--progress=no",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )
    return normalize_speedtest(json.loads(completed.stdout))


class Handler(BaseHTTPRequestHandler):
    server_version = "homelab-speedtest-probe/1"

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {BEARER_TOKEN}"
        return bool(BEARER_TOKEN) and hmac.compare_digest(supplied, expected)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path == "/health":
            self._json(
                HTTPStatus.OK,
                {"status": "busy" if _run_lock.locked() else "ready"},
            )
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != "/v1/tests/run":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            body_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            body_length = -1
        if body_length != 0:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "request_body_not_allowed"})
            return
        if not _run_lock.acquire(blocking=False):
            self._json(HTTPStatus.CONFLICT, {"error": "test_in_progress"})
            return
        try:
            self._json(HTTPStatus.OK, run_speedtest())
        except subprocess.TimeoutExpired:
            self._json(HTTPStatus.GATEWAY_TIMEOUT, {"error": "speedtest_timeout"})
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            self._json(
                HTTPStatus.BAD_GATEWAY,
                {"error": "speedtest_failed", "kind": exc.__class__.__name__},
            )
        finally:
            _run_lock.release()

    def log_message(self, format: str, *args: object) -> None:
        # Never log request headers (the bearer token lives there).
        print(f"{self.address_string()} {format % args}", flush=True)


def main() -> None:
    if not BEARER_TOKEN:
        raise SystemExit("SPEEDTEST_PROBE_TOKEN must be configured")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
