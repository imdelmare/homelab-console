import asyncio
from typing import Any

import httpx
import pytest

from app.core.settings import get_settings
from app.services.ai_manager import AIManagerError, _endpoint, request_ai_manager
from app.services.inventory import HostEntry


def _host(address: str = "192.0.2.230", ports: list[int] | None = None) -> HostEntry:
    return HostEntry(
        id="ai-host",
        name="AI Manager",
        address=address,
        kind="ai-host",
        check_ports=ports if ports is not None else [22, 8080],
    )


def test_endpoint_comes_from_private_inventory_host(monkeypatch):
    monkeypatch.setattr("app.services.ai_manager.get_host", lambda _host_id: _host())

    assert _endpoint() == "http://192.0.2.230:8080/v1/chat/completions"


@pytest.mark.parametrize(
    "host",
    [
        _host(address="8.8.8.8"),
        _host(address="127.0.0.1"),
        _host(address="ai-host.local"),
        _host(ports=[22]),
    ],
)
def test_endpoint_rejects_targets_outside_declared_boundary(monkeypatch, host):
    monkeypatch.setattr("app.services.ai_manager.get_host", lambda _host_id: host)

    with pytest.raises(AIManagerError):
        _endpoint()


async def test_chat_completion_contract_and_usage(monkeypatch):
    class Client:
        captured: tuple[str, dict[str, Any]] | None = None

        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, **kwargs):
            type(self).captured = (url, kwargs)
            return httpx.Response(
                200,
                json={
                    "model": "Qwen3.5-4B-Q8_0",
                    "choices": [{"message": {"content": '{"action":"keep"}'}}],
                    "usage": {
                        "prompt_tokens": 30,
                        "completion_tokens": 8,
                        "prompt_tokens_details": {"cached_tokens": 4},
                    },
                },
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("app.services.ai_manager._endpoint", lambda: "http://192.0.2.230:8080/v1/chat/completions")
    monkeypatch.setattr("app.services.ai_manager.httpx.AsyncClient", Client)

    result = await request_ai_manager(
        instructions="Return JSON",
        context={"task": "check DNS", "password": "must-not-leave-process"},
        schema_name="routing_test",
        schema={"type": "object"},
        max_output_tokens=100,
    )

    assert Client.captured is not None
    url, kwargs = Client.captured
    assert url == "http://192.0.2.230:8080/v1/chat/completions"
    assert kwargs["json"]["temperature"] == 0
    assert kwargs["json"]["response_format"]["json_schema"]["strict"] is True
    assert kwargs["json"]["messages"][1]["content"] == (
        '{"task": "check DNS", "password": "[REDACTED]"}'
    )
    assert result.model == "ai-manager:Qwen3.5-4B-Q8_0"
    assert (result.input_tokens, result.cached_input_tokens, result.output_tokens) == (30, 4, 8)
    assert result.telemetry["provider"] == "ai_manager"
    assert result.telemetry["fallback_used"] is False
    assert result.telemetry["prompt_version"] == "v1"
    assert result.telemetry["schema_version"] == "v1"
    assert isinstance(result.telemetry["queue_wait_ms"], int)
    assert isinstance(result.telemetry["inference_latency_ms"], int)


async def test_chat_completion_sanitizes_transport_error(monkeypatch):
    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, **kwargs):
            raise httpx.ConnectError("sensitive transport details")

    monkeypatch.setattr("app.services.ai_manager._endpoint", lambda: "http://192.0.2.230:8080/v1/chat/completions")
    monkeypatch.setattr("app.services.ai_manager.httpx.AsyncClient", Client)

    with pytest.raises(AIManagerError, match="request failed") as exc_info:
        await request_ai_manager(
            instructions="Return JSON",
            context={},
            schema_name="test",
            schema={"type": "object"},
            max_output_tokens=10,
        )
    assert "sensitive" not in str(exc_info.value)
    assert exc_info.value.telemetry["error_kind"] == "transport"


async def test_chat_completion_rejects_malformed_usage(monkeypatch):
    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, **_kwargs):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": "not-a-number"},
                },
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("app.services.ai_manager._endpoint", lambda: "http://192.0.2.230:8080/v1/chat/completions")
    monkeypatch.setattr("app.services.ai_manager.httpx.AsyncClient", Client)

    with pytest.raises(AIManagerError, match="invalid usage data") as exc_info:
        await request_ai_manager(
            instructions="Return JSON",
            context={},
            schema_name="test",
            schema={"type": "object"},
            max_output_tokens=10,
        )

    assert exc_info.value.telemetry["error_kind"] == "invalid_response"


async def test_ai_manager_serializes_inference(monkeypatch):
    class Client:
        active = 0
        max_active = 0

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, **kwargs):
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            await asyncio.sleep(0.01)
            type(self).active -= 1
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{}"}}]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("app.services.ai_manager._endpoint", lambda: "http://192.0.2.230:8080/v1/chat/completions")
    monkeypatch.setattr("app.services.ai_manager.httpx.AsyncClient", Client)

    calls = [
        request_ai_manager(
            instructions="Return JSON",
            context={"call": index},
            schema_name="test",
            schema={"type": "object"},
            max_output_tokens=10,
        )
        for index in range(2)
    ]
    results = await asyncio.gather(*calls)

    assert Client.max_active == 1
    assert results[1].telemetry["queue_wait_ms"] > 0


def test_ai_manager_defaults_match_declared_server():
    settings = get_settings()

    assert settings.ai_manager_host_id == "ai-host"
    assert settings.ai_manager_port == 8080
    assert settings.ai_manager_model == "Qwen3.5-4B-Q8_0"
