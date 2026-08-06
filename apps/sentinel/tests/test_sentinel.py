from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.config import HeartbeatConfig, SentinelConfig, TargetConfig, load_config  # noqa: E402
from sentinel.heartbeat import HeartbeatServer  # noqa: E402
from sentinel.heartbeat_client import HeartbeatClientConfig, load_client_config, send_heartbeat  # noqa: E402
from sentinel.service import SentinelService  # noqa: E402
from sentinel.store import Observation, SentinelStore  # noqa: E402
from sentinel.telegram import alert_text  # noqa: E402


ROOT = Path(__file__).resolve().parents[3]
PUBLIC_HEALTH_URL = "https://console.example.com/health"


class CaptureNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


class FailingNotifier:
    def send(self, text: str) -> bool:
        raise RuntimeError("delivery failed")


def test_sentinel_alert_uses_correct_english_singular():
    assert "1 signal requires attention." in alert_text("Console unavailable", "timeout", 1)


def test_deploy_uses_fixed_public_health_target():
    deploy_script = (ROOT / "deploy" / "deploy-sentinel-vps.sh").read_text(encoding="utf-8")
    example = json.loads((ROOT / "config" / "sentinel.example.json").read_text(encoding="utf-8"))
    public_targets = [target for target in example["targets"] if target["id"] == "api-public-health"]

    assert f'readonly PUBLIC_HEALTH_URL="{PUBLIC_HEALTH_URL}"' in deploy_script
    assert "HOMELAB_PUBLIC_HEALTH_URL" not in deploy_script
    assert len(public_targets) == 1
    assert public_targets[0]["url"] == PUBLIC_HEALTH_URL


def test_deduplicates_open_incident_and_notifies_recovery(tmp_path):
    notifier = CaptureNotifier()
    config = SentinelConfig(
        state_path=str(tmp_path / "sentinel.sqlite3"),
        recovery_confirmations=1,
        aggregation_window_seconds=0,
    )
    service = SentinelService(config, notifier=notifier)  # type: ignore[arg-type]
    failing = Observation(
        dedupe_key="http:console",
        source_id="console",
        kind="http",
        ok=False,
        title="Console failed",
        description="timeout",
    )
    healthy = Observation(
        dedupe_key="http:console",
        source_id="console",
        kind="http",
        ok=True,
        title="Console failed",
        description="HTTP 200",
    )

    assert service._handle_observation(failing)[0].action == "opened"
    service._flush_notifications()
    assert service._handle_observation(failing)[0].action == "repeated_open"
    assert len(notifier.messages) == 1

    assert service._handle_observation(healthy)[0].action == "resolved"
    service._flush_notifications()
    assert len(notifier.messages) == 2
    assert "SENTINEL · RECOVERED" in notifier.messages[1]
    assert "All correlated signals have returned to normal." in notifier.messages[1]


def test_missing_heartbeat_opens_then_resolves_after_post(tmp_path):
    notifier = CaptureNotifier()
    config = SentinelConfig(
        state_path=str(tmp_path / "sentinel.sqlite3"),
        recovery_confirmations=1,
        aggregation_window_seconds=0,
        heartbeats=[HeartbeatConfig(id="home", name="Home", timeout_seconds=60)],
    )
    service = SentinelService(config, notifier=notifier)  # type: ignore[arg-type]

    changes = service.run_once()
    assert changes[0].action == "opened"
    assert "heartbeat missing" in notifier.messages[0]

    service.store.record_heartbeat("home", "127.0.0.1", {"ok": True})
    changes = service.run_once()
    assert changes[0].action == "resolved"
    assert "SENTINEL · RECOVERED" in notifier.messages[-1]


def test_heartbeat_server_requires_token_and_records_payload(tmp_path):
    store = SentinelStore(str(tmp_path / "sentinel.sqlite3"))
    server = HeartbeatServer("127.0.0.1", 0, "secret", store)
    server.start()
    assert server._server is not None
    port = server._server.server_address[1]
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/heartbeat/home",
            data=json.dumps({"status": "ok"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Sentinel-Token": "secret"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 200
        assert store.last_heartbeat_at("home") is not None
    finally:
        server.stop()


def test_heartbeat_server_rejects_wrong_or_missing_token(tmp_path):
    store = SentinelStore(str(tmp_path / "sentinel.sqlite3"))
    server = HeartbeatServer("127.0.0.1", 0, "secret", store)
    server.start()
    assert server._server is not None
    port = server._server.server_address[1]
    try:
        for headers in ({}, {"X-Sentinel-Token": "wrong"}):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/heartbeat/home",
                data=json.dumps({"status": "ok"}).encode("utf-8"),
                headers={"Content-Type": "application/json", **headers},
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=3)
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            else:
                raise AssertionError("expected 401 for missing/wrong token")
        assert store.last_heartbeat_at("home") is None
    finally:
        server.stop()


def test_heartbeat_server_fails_closed_without_configured_token(tmp_path):
    store = SentinelStore(str(tmp_path / "sentinel.sqlite3"))
    server = HeartbeatServer("127.0.0.1", 0, "", store)
    server.start()
    assert server._server is not None
    port = server._server.server_address[1]
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/heartbeat/home",
            data=json.dumps({"status": "ok"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("expected 401 when no heartbeat token is configured")
        assert store.last_heartbeat_at("home") is None
    finally:
        server.stop()


def test_incident_reopen_resets_occurrence_count(tmp_path):
    store = SentinelStore(str(tmp_path / "sentinel.sqlite3"))
    failing = Observation(
        dedupe_key="http:console",
        source_id="console",
        kind="http",
        ok=False,
        title="Console failed",
        description="timeout",
    )
    healthy = Observation(
        dedupe_key="http:console",
        source_id="console",
        kind="http",
        ok=True,
        title="Console failed",
        description="HTTP 200",
    )
    assert store.record_failure(failing).occurrences == 1
    assert store.record_failure(failing).occurrences == 2
    assert store.record_recovery(healthy).occurrences == 2
    assert store.record_failure(failing).occurrences == 1


def test_failure_and_recovery_require_confirmations(tmp_path):
    store = SentinelStore(str(tmp_path / "sentinel.sqlite3"))
    failing = Observation(
        dedupe_key="http:console",
        source_id="console",
        kind="http",
        ok=False,
        title="Console failed",
        description="timeout",
        failure_confirmations=3,
    )
    healthy = Observation(
        dedupe_key="http:console",
        source_id="console",
        kind="http",
        ok=True,
        title="Console failed",
        description="HTTP 200",
    )

    assert store.record_failure(failing).action == "pending_failure"
    assert store.record_failure(failing).action == "pending_failure"
    assert store.record_failure(failing).action == "opened"
    assert store.record_recovery(healthy, confirmations=2).action == "pending_recovery"
    assert store.record_recovery(healthy, confirmations=2).action == "resolved"


def test_correlated_sources_send_one_group_alert(tmp_path):
    notifier = CaptureNotifier()
    service = SentinelService(
        SentinelConfig(
            state_path=str(tmp_path / "sentinel.sqlite3"),
            aggregation_window_seconds=0,
        ),
        notifier=notifier,  # type: ignore[arg-type]
    )
    for source in ("http", "heartbeat"):
        service._handle_observation(
            Observation(
                dedupe_key=f"{source}:console",
                source_id=source,
                kind=source,
                ok=False,
                title=f"{source} failed",
                description="timeout",
                notification_group="availability",
            )
        )

    service._flush_notifications()

    assert len(notifier.messages) == 1
    assert notifier.messages[0].startswith("🚨 SENTINEL · ALERT")
    assert "Homelab availability degraded" in notifier.messages[0]
    assert "2 signals require attention." in notifier.messages[0]
    assert "• http failed\n  timeout" in notifier.messages[0]
    assert "• heartbeat failed\n  timeout" in notifier.messages[0]
    assert notifier.messages[0].endswith("Status: active")


def test_http_target_status_range(monkeypatch, tmp_path):
    from sentinel import checks

    class Response:
        status = 503

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(checks.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    config = SentinelConfig(
        state_path=str(tmp_path / "sentinel.sqlite3"),
        targets=[TargetConfig(id="console", name="Console", url="https://example.invalid/health")],
    )
    service = SentinelService(config, notifier=CaptureNotifier())  # type: ignore[arg-type]
    changes = service.run_once()
    assert changes[0].action == "pending_failure"
    assert "HTTP 503" in changes[0].observation.description


def test_explicit_missing_config_path_fails(tmp_path):
    missing = tmp_path / "missing.json"
    try:
        load_config(str(missing), environ={})
    except FileNotFoundError as exc:
        assert str(missing) in str(exc)
    else:
        raise AssertionError("missing explicit config path should fail")


def test_notifier_failure_does_not_break_incident_loop(tmp_path):
    config = SentinelConfig(state_path=str(tmp_path / "sentinel.sqlite3"))
    service = SentinelService(config, notifier=FailingNotifier())  # type: ignore[arg-type]
    failing = Observation(
        dedupe_key="http:console",
        source_id="console",
        kind="http",
        ok=False,
        title="Console failed",
        description="timeout",
    )
    changes = service._handle_observation(failing)
    assert changes[0].action == "opened"


def test_heartbeat_client_loads_required_env():
    config = load_client_config(
        {
            "SENTINEL_HEARTBEAT_URL": "https://sentinel.example.com/heartbeat/home",
            "SENTINEL_HEARTBEAT_TOKEN": "secret",
        }
    )
    assert config.url.endswith("/heartbeat/home")
    assert config.token == "secret"
    assert config.source == "homelab-console"


def test_heartbeat_client_posts_to_server(tmp_path):
    store = SentinelStore(str(tmp_path / "sentinel.sqlite3"))
    server = HeartbeatServer("127.0.0.1", 0, "secret", store)
    server.start()
    assert server._server is not None
    port = server._server.server_address[1]
    try:
        result = send_heartbeat(
            HeartbeatClientConfig(
                url=f"http://127.0.0.1:{port}/heartbeat/home",
                token="secret",
                source="homelab-console",
            ),
            payload={"status": "ok"},
        )
        assert result["ok"] is True
        assert store.last_heartbeat_at("home") is not None
    finally:
        server.stop()
