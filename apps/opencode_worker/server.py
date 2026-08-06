"""Authenticated loopback dispatcher for the dedicated OpenCode worker."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

logger = logging.getLogger("homelab.opencode_worker")

TASK_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
MAX_BODY_BYTES = 4096
CHILD_ENV_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "OPENCODE_CONFIG",
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS",
    "OPENCODE_DISABLE_PROJECT_CONFIG",
    "OPENCODE_PURE",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
}


class WorkerSupervisor:
    def __init__(
        self,
        *,
        project_dir: Path,
        opencode_bin: str = "opencode",
        agent: str = "homelab-fixer",
        timeout_seconds: float = 1800,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.project_dir = project_dir.resolve()
        self.opencode_bin = opencode_bin
        self.agent = agent
        self.timeout_seconds = max(30.0, min(float(timeout_seconds), 7200.0))
        self._popen = popen
        self._lock = threading.Lock()
        self._active: dict[str, subprocess.Popen] = {}

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def launch(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._active:
                return False

            process = self._popen(
                self._command(task_id),
                cwd=self.project_dir,
                env=self._child_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            self._active[task_id] = process

        threading.Thread(
            target=self._monitor,
            args=(task_id, process),
            name=f"opencode-worker-{task_id[:8]}",
            daemon=True,
        ).start()
        return True

    def _command(self, task_id: str) -> list[str]:
        prompt = (
            "You are Fixer, powered by OpenCode, operating under docs/OPENCODE_WORKER.md.\n"
            f"Dispatched task_id: {task_id}\n\n"
            "Retrieve this exact task from Homelab Console MCP. Do not claim open work. "
            "Verify ownership and explicit dispatch authorization before doing anything. "
            "Use the task as the canonical communication channel."
        )
        return [
            self.opencode_bin,
            "--pure",
            "run",
            "--agent",
            self.agent,
            "--format",
            "json",
            "--dir",
            str(self.project_dir),
            prompt,
        ]

    @staticmethod
    def _child_env() -> dict[str, str]:
        return {key: value for key, value in os.environ.items() if key in CHILD_ENV_KEYS}

    def _monitor(self, task_id: str, process: subprocess.Popen) -> None:
        try:
            process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            logger.warning("OpenCode worker timed out task_id=%s", task_id)
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        finally:
            with self._lock:
                if self._active.get(task_id) is process:
                    self._active.pop(task_id, None)


def create_server(
    supervisor: WorkerSupervisor,
    *,
    secret: str,
    host: str = "127.0.0.1",
    port: int = 8767,
) -> ThreadingHTTPServer:
    if not secret:
        raise ValueError("FIXER_DISPATCH_SECRET is required")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("OpenCode worker must bind to loopback")

    class Handler(BaseHTTPRequestHandler):
        server_version = "HomelabOpenCodeWorker/1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler API.
            if self.path != "/health":
                self._reply(404, {"ok": False, "code": "not_found"})
                return
            self._reply(200, {"ok": True, "active": supervisor.active_count()})

        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler API.
            if self.path != "/fixer":
                self._reply(404, {"ok": False, "code": "not_found"})
                return
            supplied = self.headers.get("X-Secret", "")
            if not hmac.compare_digest(supplied.encode("utf-8"), secret.encode("utf-8")):
                self._discard_body()
                self._reply(403, {"ok": False, "code": "unauthorized"})
                return
            payload = self._payload()
            if payload is None:
                return
            task_id = payload.get("task_id")
            if set(payload) != {"task_id"} or not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
                self._reply(400, {"ok": False, "code": "invalid_input"})
                return
            try:
                launched = supervisor.launch(task_id)
            except (OSError, ValueError):
                logger.exception("OpenCode worker spawn failed task_id=%s", task_id)
                self._reply(503, {"ok": False, "code": "spawn_failed"})
                return
            if not launched:
                self._reply(409, {"ok": False, "code": "already_running"})
                return
            self._reply(202, {"ok": True, "code": "accepted"})

        def _payload(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._reply(400, {"ok": False, "code": "invalid_input"})
                return None
            try:
                decoded = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._reply(400, {"ok": False, "code": "invalid_json"})
                return None
            if not isinstance(decoded, dict):
                self._reply(400, {"ok": False, "code": "invalid_input"})
                return None
            return decoded

        def _discard_body(self) -> None:
            try:
                length = min(int(self.headers.get("Content-Length", "0")), MAX_BODY_BYTES)
            except ValueError:
                return
            if length > 0:
                self.rfile.read(length)

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    logging.basicConfig(level=os.environ.get("OPENCODE_WORKER_LOG_LEVEL", "INFO"))
    project_dir = Path(os.environ.get("OPENCODE_WORKER_PROJECT_DIR", Path(__file__).resolve().parents[2]))
    supervisor = WorkerSupervisor(
        project_dir=project_dir,
        opencode_bin=os.environ.get("OPENCODE_WORKER_BIN", "opencode"),
        agent=os.environ.get("OPENCODE_WORKER_AGENT", "homelab-fixer"),
        timeout_seconds=float(os.environ.get("OPENCODE_WORKER_TIMEOUT_SECONDS", "1800")),
    )
    server = create_server(
        supervisor,
        secret=os.environ.get("FIXER_DISPATCH_SECRET", ""),
        host=os.environ.get("OPENCODE_WORKER_HOST", "127.0.0.1"),
        port=int(os.environ.get("OPENCODE_WORKER_PORT", "8767")),
    )
    logger.info("OpenCode worker listening on loopback port %s", server.server_port)
    server.serve_forever()


if __name__ == "__main__":
    main()
