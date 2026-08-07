import httpx
import pytest

from app.providers.cloudflaretunnel.client import CloudflareTunnelClient
from app.providers.cloudflaretunnel.tools import (
    CloudflareTunnelProvider,
    all_tunnels_status,
    connectors_list,
)
from app.providers.errors import ProviderError
from app.tools.registry import get_tool
from app.tools.summaries import cloudflare_summary

ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
TUNNEL_ID = "11111111-2222-4333-8444-555555555555"


def _configure(monkeypatch, *, tunnel_ids=None, token="read-token"):
    monkeypatch.setattr(
        "app.providers.cloudflaretunnel.client.provider_config",
        lambda _provider_id: {
            "account_id": ACCOUNT_ID,
            "tunnel_ids": [TUNNEL_ID] if tunnel_ids is None else tunnel_ids,
            "timeout_seconds": 8,
            "base_url": "https://attacker.invalid",
            "verify_tls": False,
        },
    )
    monkeypatch.setattr(
        "app.providers.cloudflaretunnel.client.get_provider_secrets",
        lambda _provider_id: {"bearer_token": token},
    )


def _mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        assert kwargs.pop("verify") is True
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.httpclient.httpx.AsyncClient", client_factory)


def _tunnel_payload(status="healthy"):
    return {
        "success": True,
        "result": {
            "id": TUNNEL_ID,
            "name": "homelab",
            "status": status,
            "config_src": "cloudflare",
            "conns_active_at": "2026-07-17T08:00:00Z",
            "conns_inactive_at": None,
            "account_tag": ACCOUNT_ID,
            "metadata": {"private": "discarded"},
            "connections": [{"origin_ip": "discarded"}],
        },
    }


def _connections_payload():
    return {
        "success": True,
        "result": [
            {
                "id": "connector-private-id",
                "version": "2026.7.0",
                "arch": "amd64",
                "run_at": "2026-07-17T08:00:00Z",
                "features": ["quic"],
                "conns": [
                    {
                        "id": "connection-private-id",
                        "origin_ip": "198.51.100.20",
                        "colo_name": "FCO",
                        "is_pending_reconnect": False,
                    },
                    {
                        "id": "pending-private-id",
                        "origin_ip": "198.51.100.20",
                        "is_pending_reconnect": True,
                    },
                ],
            }
        ],
    }


async def test_api_tools_use_fixed_origin_declared_paths_and_bearer_auth(monkeypatch):
    _configure(monkeypatch)
    paths: list[str] = []

    def handler(request: httpx.Request):
        assert request.url.host == "api.cloudflare.com"
        assert request.headers["authorization"] == "Bearer read-token"
        paths.append(request.url.path)
        if request.url.path.endswith("/connections"):
            return httpx.Response(200, json=_connections_payload())
        return httpx.Response(200, json=_tunnel_payload())

    _mock_transport(monkeypatch, handler)
    tunnels = await all_tunnels_status()
    connectors = await connectors_list()

    assert tunnels["total"] == 1
    assert tunnels["by_status"] == {
        "healthy": 1,
        "degraded": 0,
        "unavailable": 0,
    }
    assert connectors["connectors_total"] == 1
    assert connectors["connections_active"] == 1
    assert connectors["connections_pending_reconnect"] == 1
    assert paths == [
        f"/client/v4/accounts/{ACCOUNT_ID}/cfd_tunnel/{TUNNEL_ID}",
        f"/client/v4/accounts/{ACCOUNT_ID}/cfd_tunnel/{TUNNEL_ID}/connections",
    ]


async def test_api_tools_fall_back_to_unified_tunnel_paths_on_legacy_400(monkeypatch):
    _configure(monkeypatch)
    paths: list[str] = []

    def handler(request: httpx.Request):
        paths.append(request.url.path)
        if "/cfd_tunnel/" in request.url.path:
            return httpx.Response(400, json={"success": False})
        if request.url.path.endswith("/connections"):
            return httpx.Response(200, json=_connections_payload())
        return httpx.Response(200, json=_tunnel_payload())

    _mock_transport(monkeypatch, handler)

    assert (await all_tunnels_status())["total"] == 1
    assert (await connectors_list())["connectors_total"] == 1
    assert paths == [
        f"/client/v4/accounts/{ACCOUNT_ID}/cfd_tunnel/{TUNNEL_ID}",
        f"/client/v4/accounts/{ACCOUNT_ID}/tunnels/{TUNNEL_ID}",
        f"/client/v4/accounts/{ACCOUNT_ID}/cfd_tunnel/{TUNNEL_ID}/connections",
        f"/client/v4/accounts/{ACCOUNT_ID}/tunnels/{TUNNEL_ID}/connections",
    ]


async def test_cloudflare_errors_expose_only_bounded_code_and_message(monkeypatch):
    _configure(monkeypatch)

    def handler(_request: httpx.Request):
        return httpx.Response(
            400,
            json={
                "errors": [
                    {
                        "code": 1001,
                        "message": "invalid account identifier",
                        "private": "discarded",
                    }
                ],
                "result": {"token": "discarded"},
            },
        )

    _mock_transport(monkeypatch, handler)

    with pytest.raises(ProviderError) as exc_info:
        await CloudflareTunnelClient().tunnel_status(TUNNEL_ID)

    assert exc_info.value.code == "invalid_response"
    assert "1001: invalid account identifier" in exc_info.value.message
    assert "private" not in exc_info.value.message
    assert "token" not in exc_info.value.message


@pytest.mark.parametrize("errors", [{"code": 1001}, "invalid account"])
async def test_cloudflare_malformed_errors_preserve_http_failure(monkeypatch, errors):
    _configure(monkeypatch)
    _mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(400, json={"errors": errors}),
    )

    with pytest.raises(ProviderError) as exc_info:
        await CloudflareTunnelClient().tunnel_status(TUNNEL_ID)

    assert exc_info.value.code == "invalid_response"
    assert exc_info.value.message == "cloudflaretunnel returned HTTP 400"


async def test_connector_output_discards_ids_origin_ips_and_colos(monkeypatch):
    _configure(monkeypatch)
    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=_connections_payload()),
    )

    result = await connectors_list()
    connector = result["connectors"][0]

    assert connector == {
        "tunnel_id": TUNNEL_ID,
        "version": "2026.7.0",
        "architecture": "amd64",
        "run_at": "2026-07-17T08:00:00Z",
        "features": ["quic"],
        "connections_total": 2,
        "connections_active": 1,
        "connections_pending_reconnect": 1,
    }
    assert "origin_ip" not in connector
    assert "colo_name" not in connector
    assert "id" not in connector


async def test_client_rejects_undeclared_tunnel_without_request(monkeypatch):
    _configure(monkeypatch)

    with pytest.raises(ProviderError) as exc_info:
        await CloudflareTunnelClient().tunnel_status(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        )

    assert exc_info.value.code == "configuration_missing"
    assert "aaaaaaaa" not in exc_info.value.message


@pytest.mark.parametrize("tunnel_ids", [["../../token"], "not-a-list"])
def test_client_rejects_invalid_tunnel_configuration(monkeypatch, tunnel_ids):
    _configure(monkeypatch, tunnel_ids=tunnel_ids)

    with pytest.raises(ProviderError) as exc_info:
        CloudflareTunnelClient()

    assert exc_info.value.code == "configuration_missing"


async def test_provider_health_maps_cloudflare_state(monkeypatch):
    _configure(monkeypatch)
    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=_tunnel_payload("down")),
    )

    health = await CloudflareTunnelProvider().health()

    assert health.status == "unavailable"
    assert "down or inactive" in health.detail


async def test_summary_creates_connector_findings(monkeypatch):
    _configure(monkeypatch)

    def handler(request: httpx.Request):
        if request.url.path.endswith("/connections"):
            return httpx.Response(200, json={"success": True, "result": []})
        return httpx.Response(200, json=_tunnel_payload("healthy"))

    _mock_transport(monkeypatch, handler)
    result = await cloudflare_summary()

    assert result["summary"]["status"] == "degraded"
    assert result["summary"]["severity"] == "critical"
    assert result["summary"]["metrics"]["connections_active"] == 0
    assert any(
        "No active cloudflared" in finding["message"]
        for finding in result["summary"]["findings"]
    )


def test_registry_exposes_three_cloudflare_api_tools():
    tool_ids = {
        "cloudflare.tunnels.status",
        "cloudflare.connectors.list",
        "cloudflare.summary",
    }
    tools = [get_tool(tool_id) for tool_id in tool_ids]

    assert all(tool is not None for tool in tools)
    assert all(tool.provider_id == "cloudflaretunnel" for tool in tools if tool)
    assert all(tool.mode == "read" and tool.risk == "low" for tool in tools if tool)
    assert all(tool.input_model.model_json_schema()["properties"] == {} for tool in tools if tool)
