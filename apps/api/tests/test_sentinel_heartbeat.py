import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.providers.errors import ProviderError
from app.services.inventory import SentinelHeartbeatEntry
from app.services import sentinel_heartbeat

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        kwargs.pop("verify", None)
        return _REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.httpclient.httpx.AsyncClient", client_factory)


@pytest.fixture(autouse=True)
def reset_state():
    sentinel_heartbeat.reset_state_for_tests()
    yield
    sentinel_heartbeat.reset_state_for_tests()


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://10.0.0.1:8766",
        "http://example:example@10.0.0.1:8766",
        "http://10.0.0.1:8766/heartbeat/home",
        "http://10.0.0.1:8766?source=home",
        "http://sentinel.example.com:8766",
        "http://10.0.0.1:not-a-port",
        "http://10.0.0.1:0",
        "http://10.0.0.1:70000",
    ],
)
def test_inventory_rejects_arbitrary_or_unsafe_endpoint_shapes(base_url):
    with pytest.raises(ValidationError):
        SentinelHeartbeatEntry(base_url=base_url, source_id="home")


def test_inventory_accepts_private_http_and_public_https():
    private = SentinelHeartbeatEntry(base_url="http://10.0.0.1:8766/", source_id="home")
    public = SentinelHeartbeatEntry(base_url="https://sentinel.example.com", source_id="home")

    assert private.base_url == "http://10.0.0.1:8766"
    assert public.base_url == "https://sentinel.example.com"


async def test_client_sends_fixed_authenticated_v1_payload(monkeypatch):
    token = "test-heartbeat-token-value"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://10.0.0.1:8766/heartbeat/home"
        assert request.headers["authorization"] == f"Bearer {token}"
        payload = json.loads(request.content)
        assert payload["source"] == "homelab-console-api"
        assert payload["sent_at"]
        assert payload["host"]
        assert payload["platform"]
        return httpx.Response(200, json={"ok": True})

    _mock_transport(monkeypatch, handler)
    entry = SentinelHeartbeatEntry(base_url="http://10.0.0.1:8766", source_id="home")

    await sentinel_heartbeat.send_once(
        sentinel_heartbeat.SentinelHeartbeatClient(entry, token)
    )

    status = sentinel_heartbeat.heartbeat_status()
    assert status["last_success_at"]
    assert status["last_error"] == ""
    assert token not in json.dumps(status)


def test_client_disables_environment_proxy_inheritance():
    entry = SentinelHeartbeatEntry(base_url="http://10.0.0.1:8766", source_id="home")

    assert sentinel_heartbeat.SentinelHeartbeatClient(entry, "token").trust_env is False


async def test_client_normalizes_auth_and_invalid_response(monkeypatch):
    entry = SentinelHeartbeatEntry(base_url="http://10.0.0.1:8766", source_id="home")
    client = sentinel_heartbeat.SentinelHeartbeatClient(entry, "wrong-token")

    _mock_transport(monkeypatch, lambda _request: httpx.Response(401, json={"ok": False}))
    with pytest.raises(ProviderError) as auth_error:
        await client.send({"source": "test"})
    assert auth_error.value.code == "auth_failed"
    assert "wrong-token" not in str(auth_error.value)

    _mock_transport(monkeypatch, lambda _request: httpx.Response(200, json={"ok": False}))
    with pytest.raises(ProviderError) as response_error:
        await client.send({"source": "test"})
    assert response_error.value.code == "invalid_response"


def test_enabled_configuration_requires_inventory_and_token(monkeypatch):
    monkeypatch.setattr(
        sentinel_heartbeat,
        "get_settings",
        lambda: SimpleNamespace(sentinel_heartbeat_token=""),
    )
    with pytest.raises(RuntimeError, match="SENTINEL_HEARTBEAT_TOKEN"):
        sentinel_heartbeat.validate_configuration()

    monkeypatch.setattr(
        sentinel_heartbeat,
        "get_settings",
        lambda: SimpleNamespace(sentinel_heartbeat_token="configured"),
    )
    monkeypatch.setattr(sentinel_heartbeat, "get_sentinel_heartbeat", lambda: None)
    with pytest.raises(RuntimeError, match="providers.vps.sentinel_heartbeat"):
        sentinel_heartbeat.validate_configuration()


async def test_worker_continues_after_normalized_failure_and_cancels(monkeypatch, caplog):
    entry = SentinelHeartbeatEntry(base_url="http://10.0.0.1:8766", source_id="home")
    token = "never-log-this-token"
    attempts = 0

    monkeypatch.setattr(sentinel_heartbeat, "validate_configuration", lambda: (entry, token))
    monkeypatch.setattr(sentinel_heartbeat, "SentinelHeartbeatClient", lambda *_args: object())

    async def fake_send_once(_client):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderError("unreachable", "fixed normalized failure")
        raise asyncio.CancelledError

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(sentinel_heartbeat, "send_once", fake_send_once)
    monkeypatch.setattr(sentinel_heartbeat.asyncio, "sleep", no_wait)

    with pytest.raises(asyncio.CancelledError):
        await sentinel_heartbeat.worker_loop()

    assert attempts == 2
    assert sentinel_heartbeat.heartbeat_status()["enabled"] is False
    assert token not in caplog.text
