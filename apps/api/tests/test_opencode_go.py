import httpx
import pytest

from app.core.settings import get_settings
from app.services.opencode_go import OpenCodeGoError, request_structured_decision


async def test_direct_client_uses_fixed_endpoint_and_bearer(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "opencode_go_api_key", "secret-key")
    captured = {}

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return httpx.Response(
                200,
                headers={"x-request-id": "request-1"},
                json={
                    "model": "deepseek-v4-pro",
                    "choices": [{"message": {"content": '{"action":"keep"}'}}],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 4,
                        "prompt_tokens_details": {"cached_tokens": 3},
                        "completion_tokens_details": {"reasoning_tokens": 2},
                    },
                },
            )

    monkeypatch.setattr("app.services.opencode_go.httpx.AsyncClient", Client)
    result = await request_structured_decision(
        model="deepseek-v4-pro",
        instructions="Classify",
        context={"task": {"title": "Gateway"}},
        schema_name="task_router_decision",
        schema={"type": "object"},
        max_output_tokens=500,
        timeout_seconds=30,
    )

    assert captured["url"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["client"]["follow_redirects"] is False
    assert captured["payload"]["model"] == "deepseek-v4-pro"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert result.output_text == '{"action":"keep"}'
    assert (result.input_tokens, result.cached_input_tokens, result.output_tokens) == (12, 3, 4)
    assert result.reasoning_tokens == 2
    assert result.provider_request_id == "request-1"


async def test_direct_client_rejects_unreviewed_model(monkeypatch):
    monkeypatch.setattr(get_settings(), "opencode_go_api_key", "secret-key")

    with pytest.raises(OpenCodeGoError, match="allowlisted") as exc_info:
        await request_structured_decision(
            model="caller-selected-model",
            instructions="Classify",
            context={},
            schema_name="decision",
            schema={"type": "object"},
            max_output_tokens=100,
            timeout_seconds=10,
        )

    assert exc_info.value.error_kind == "invalid_model"


async def test_direct_client_retries_only_transient_status(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "opencode_go_api_key", "secret-key")
    monkeypatch.setattr(settings, "opencode_go_max_attempts", 3)
    statuses = [503, 200]

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            status = statuses.pop(0)
            if status == 200:
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "{}"}}]},
                )
            return httpx.Response(status)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.services.opencode_go.httpx.AsyncClient", Client)
    monkeypatch.setattr("app.services.opencode_go.asyncio.sleep", no_sleep)

    result = await request_structured_decision(
        model="grok-4.5",
        instructions="Reply",
        context={},
        schema_name="chat",
        schema={"type": "object"},
        max_output_tokens=100,
        timeout_seconds=10,
    )

    assert result.output_text == "{}"
    assert statuses == []
