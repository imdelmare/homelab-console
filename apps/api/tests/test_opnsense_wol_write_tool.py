"""Focused tests for the governed OPNsense WoL capability."""

import httpx
import pytest
from pydantic import ValidationError

from app.providers.errors import ProviderError
from app.providers.opnsense import tools as opnsense_tools
from app.tools.governance import APPROVED_WRITE_TOOLS
from app.tools.registry import WolWakeInput, get_tool


def _configure(monkeypatch, targets):
    secrets = {
        "base_url": "https://opnsense.test",
        "api_key": "key",
        "api_secret": "secret",
        "wol_api_key": "wol-key",
        "wol_api_secret": "wol-secret",
        "verify_tls": True,
    }
    monkeypatch.setattr(
        "app.providers.opnsense.client.get_provider_secrets",
        lambda _provider_id: secrets,
    )
    monkeypatch.setattr(
        opnsense_tools,
        "provider_config",
        lambda _provider_id: {"wol_targets": targets},
    )


def _mock_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("verify", None)
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.httpclient.httpx.AsyncClient", client_factory)


def test_wol_tool_is_active_under_adr_0008():
    assert (
        APPROVED_WRITE_TOOLS["opnsense.wol.wake"]
        == "docs/decisions/0008-activate-opnsense-wol-drill.md"
    )
    tool = get_tool("opnsense.wol.wake")
    assert tool is not None and tool.enabled is True


def test_wol_input_is_strict_and_never_accepts_network_parameters():
    assert WolWakeInput.model_validate({"target_id": "tv-salone"}).target_id == "tv-salone"
    with pytest.raises(ValidationError):
        WolWakeInput.model_validate(
            {"target_id": "tv-salone", "mac": "aa:bb:cc:dd:ee:ff"}
        )


def test_wol_client_requires_scoped_credentials(monkeypatch):
    monkeypatch.setattr(
        "app.providers.opnsense.client.get_provider_secrets",
        lambda _provider_id: {"api_key": "admin", "api_secret": "admin-secret"},
    )
    monkeypatch.setattr(
        "app.providers.opnsense.client.provider_config",
        lambda _provider_id: {"base_url": "https://opnsense.test"},
    )
    monkeypatch.setattr(
        "app.providers.opnsense.client.load_credentials_env",
        lambda: {},
    )

    client = opnsense_tools.OpnsenseClient("wol")
    assert client.has_credentials() is False
    assert "wol_api_key/wol_api_secret" in client.credentials_error()


async def test_wol_resolves_target_to_opnsense_uuid(monkeypatch):
    _configure(
        monkeypatch,
        [{"id": "pc", "uuid": "11111111-2222-3333-4444-555555555555"}],
    )

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/wol/wol/set"
        assert request.headers["authorization"].startswith("Basic ")
        assert request.read() == b'{"uuid":"11111111-2222-3333-4444-555555555555"}'
        return httpx.Response(200, json={"status": "OK"})

    _mock_http(monkeypatch, handler)
    result = await opnsense_tools.wol_wake("pc")
    assert result == {"target_id": "pc", "sent": True, "provider": "opnsense"}


async def test_wol_rejects_undeclared_or_ambiguous_target(monkeypatch):
    _configure(
        monkeypatch,
        [{"id": "pc", "uuid": "one"}, {"id": "pc", "uuid": "two"}],
    )
    with pytest.raises(ProviderError) as exc_info:
        await opnsense_tools.wol_wake("pc")
    assert exc_info.value.code == "permission_denied"

    with pytest.raises(ProviderError):
        await opnsense_tools.wol_wake("tv")


async def test_wol_normalizes_plugin_failure(monkeypatch):
    _configure(
        monkeypatch,
        [{"id": "pc", "uuid": "11111111-2222-3333-4444-555555555555"}],
    )
    _mock_http(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json={"status": "error", "error_msg": "vendor detail"},
        ),
    )
    with pytest.raises(ProviderError) as exc_info:
        await opnsense_tools.wol_wake("pc")
    assert exc_info.value.code == "degraded"
    assert "vendor detail" not in exc_info.value.message
