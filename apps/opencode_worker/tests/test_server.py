import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from opencode_worker.server import WorkerSupervisor, create_server

TASK_ID = "123e4567-e89b-42d3-a456-426614174000"


class FakeProcess:
    def wait(self, timeout=None):
        return 0

    def terminate(self):
        return None

    def kill(self):
        return None


class RecordingPopen:
    def __init__(self):
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return FakeProcess()


def request(server, path="/fixer", *, secret="test-secret", payload=None):
    body = json.dumps(payload if payload is not None else {"task_id": TASK_ID}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}{path}",
        data=body,
        headers={"Content-Type": "application/json", "X-Secret": secret},
        method="POST",
    )
    try:
        response = urllib.request.urlopen(req, timeout=2)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    return response.status, json.loads(response.read())


@pytest.fixture
def running_server(tmp_path):
    popen = RecordingPopen()
    supervisor = WorkerSupervisor(project_dir=tmp_path, popen=popen)
    server = create_server(supervisor, secret="test-secret", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, supervisor, popen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dispatch_requires_secret(running_server):
    server, _supervisor, popen = running_server

    status, body = request(server, secret="wrong")

    assert status == 403
    assert body == {"ok": False, "code": "unauthorized"}
    assert popen.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"task_id": "not-a-uuid"},
        {"task_id": TASK_ID, "command": "id"},
        [TASK_ID],
    ],
)
def test_dispatch_rejects_non_narrow_payload(running_server, payload):
    server, _supervisor, popen = running_server

    status, body = request(server, payload=payload)

    assert status == 400
    assert body["ok"] is False
    assert popen.calls == []


def test_dispatch_spawns_exact_opencode_argv_with_filtered_environment(running_server, monkeypatch):
    server, _supervisor, popen = running_server
    monkeypatch.setenv("DATABASE_URL", "must-not-reach-child")
    monkeypatch.setenv("SECRETS_PATH", "must-not-reach-child")

    status, body = request(server)

    assert status == 202
    assert body == {"ok": True, "code": "accepted"}
    command, kwargs = popen.calls[0]
    assert command[:8] == [
        "opencode",
        "--pure",
        "run",
        "--agent",
        "homelab-fixer",
        "--format",
        "json",
        "--dir",
    ]
    assert command[8] == str(Path(kwargs["cwd"]))
    assert f"Dispatched task_id: {TASK_ID}" in command[9]
    assert "DATABASE_URL" not in kwargs["env"]
    assert "SECRETS_PATH" not in kwargs["env"]
    assert kwargs["stdin"] is not None
    assert kwargs["stdout"] is not None
    assert kwargs["stderr"] is not None


def test_duplicate_active_task_returns_conflict(tmp_path):
    class BlockingProcess(FakeProcess):
        def __init__(self):
            self.release = threading.Event()

        def wait(self, timeout=None):
            self.release.wait(timeout=timeout)
            return 0

    process = BlockingProcess()
    supervisor = WorkerSupervisor(project_dir=tmp_path, popen=lambda *_args, **_kwargs: process)
    server = create_server(supervisor, secret="test-secret", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert request(server)[0] == 202
        status, body = request(server)
        assert status == 409
        assert body == {"ok": False, "code": "already_running"}
    finally:
        process.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_completed_process_releases_task_lock(tmp_path):
    process = FakeProcess()
    supervisor = WorkerSupervisor(project_dir=tmp_path, popen=lambda *_args, **_kwargs: process)

    assert supervisor.launch(TASK_ID) is True
    for _attempt in range(50):
        if supervisor.active_count() == 0:
            break
        time.sleep(0.01)

    assert supervisor.active_count() == 0
    assert supervisor.launch(TASK_ID) is True


def test_spawn_failure_returns_service_unavailable(tmp_path):
    def fail_spawn(*_args, **_kwargs):
        raise FileNotFoundError("opencode")

    supervisor = WorkerSupervisor(project_dir=tmp_path, popen=fail_spawn)
    server = create_server(supervisor, secret="test-secret", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = request(server)
        assert status == 503
        assert body == {"ok": False, "code": "spawn_failed"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_health_exposes_only_liveness(running_server):
    server, _supervisor, _popen = running_server

    with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=2) as response:
        body = json.loads(response.read())

    assert response.status == 200
    assert body == {"ok": True, "active": 0}


def test_non_loopback_bind_is_rejected(tmp_path):
    supervisor = WorkerSupervisor(project_dir=tmp_path, popen=RecordingPopen())

    with pytest.raises(ValueError, match="loopback"):
        create_server(supervisor, secret="test-secret", host="0.0.0.0")
