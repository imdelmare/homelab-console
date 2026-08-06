from unittest.mock import AsyncMock
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.providers.api_ready.tools import (
    _normalized_cloudflare_tunnel,
    _normalized_health,
    _normalized_speedtest,
    health_status,
    speedtest_run,
)
from app.providers.errors import ProviderError
from app.services.inventory import CLOUDFLARE_API_BASE_URL, ApiProviderInstanceEntry


TUNNEL_ID = "11111111-2222-4333-8444-555555555555"


def _instance() -> ApiProviderInstanceEntry:
    return ApiProviderInstanceEntry(
        id="example_service",
        name="Example Service",
        driver="json_health_v1",
        base_url="https://service.example.test",
    )


def _cloudflare_instance() -> ApiProviderInstanceEntry:
    return ApiProviderInstanceEntry(
        id="cloudflare_home",
        name="Cloudflare Home Tunnel",
        driver="cloudflare_tunnel_v1",
        account_id="0123456789abcdef0123456789abcdef",
        tunnel_id=TUNNEL_ID,
    )


def _speedtest_instance() -> ApiProviderInstanceEntry:
    return ApiProviderInstanceEntry(
        id="home_speedtest",
        name="Home Speedtest",
        driver="speedtest_probe_v1",
        base_url="http://10.0.0.70:8780",
        verify_tls=False,
        timeout_seconds=140,
    )


def test_api_ready_instance_schema_allows_only_allowlisted_driver_contracts():
    assert _instance().driver == "json_health_v1"
    with pytest.raises(ValidationError):
        ApiProviderInstanceEntry(
            id="bad-service",
            driver=cast(Any, "generic_http"),
            base_url="https://service.example.test",
        )
    with pytest.raises(ValidationError):
        ApiProviderInstanceEntry(
            id="embedded_secret",
            driver="json_health_v1",
            base_url="https://user:password@service.example.test",
        )


def test_cloudflare_driver_uses_only_the_official_api_contract():
    instance = _cloudflare_instance()
    assert instance.base_url == CLOUDFLARE_API_BASE_URL
    assert instance.verify_tls is True

    with pytest.raises(ValidationError, match="fixed Cloudflare API"):
        ApiProviderInstanceEntry(
            id="cloudflare_bad_url",
            driver="cloudflare_tunnel_v1",
            base_url="https://proxy.example.test",
            account_id="0123456789abcdef0123456789abcdef",
            tunnel_id=TUNNEL_ID,
        )
    with pytest.raises(ValidationError, match="requires TLS verification"):
        ApiProviderInstanceEntry(
            id="cloudflare_bad_tls",
            driver="cloudflare_tunnel_v1",
            account_id="0123456789abcdef0123456789abcdef",
            tunnel_id=TUNNEL_ID,
            verify_tls=False,
        )
    with pytest.raises(ValidationError, match="tunnel_id must be a UUID"):
        ApiProviderInstanceEntry(
            id="cloudflare_bad_id",
            driver="cloudflare_tunnel_v1",
            account_id="0123456789abcdef0123456789abcdef",
            tunnel_id="../../token",
        )


def test_speedtest_driver_rejects_insecure_public_endpoints():
    with pytest.raises(ValidationError, match="private endpoint"):
        ApiProviderInstanceEntry(
            id="public_speedtest",
            driver="speedtest_probe_v1",
            base_url="http://speedtest.example.com",
            verify_tls=False,
        )


@pytest.mark.parametrize(
    ("reported", "expected"),
    [("ok", "healthy"), ("ready", "healthy"), ("warning", "degraded"), ("down", "unavailable")],
)
def test_json_health_v1_normalizes_only_the_status_field(reported, expected):
    assert _normalized_health({"status": reported, "secret": "not returned"}) == (
        expected,
        reported,
    )


async def test_health_tool_uses_declared_instance_and_fixed_health_path(monkeypatch):
    request = AsyncMock(return_value={"status": "ok", "extra": {"private": "discarded"}})
    monkeypatch.setattr(
        "app.providers.api_ready.tools.get_api_provider_instance",
        lambda instance_id: _instance() if instance_id == "example_service" else None,
    )
    monkeypatch.setattr("app.providers.api_ready.tools.JsonHealthV1Client.get", request)

    result = await health_status("example_service")

    request.assert_awaited_once_with("/health")
    assert result == {
        "instance_id": "example_service",
        "status": "healthy",
        "reported_status": "ok",
    }


def test_cloudflare_tunnel_normalizer_returns_only_allowlisted_fields():
    result = _normalized_cloudflare_tunnel(
        {
            "success": True,
            "result": {
                "id": TUNNEL_ID,
                "name": "home",
                "status": "healthy",
                "config_src": "cloudflare",
                "conns_active_at": "2026-07-16T10:00:00Z",
                "conns_inactive_at": None,
                "metadata": {"secret": "discarded"},
                "connections": [{"origin_ip": "discarded"}],
            },
        },
        "cloudflare_home",
    )

    assert result == {
        "instance_id": "cloudflare_home",
        "tunnel_id": TUNNEL_ID,
        "name": "home",
        "status": "healthy",
        "reported_status": "healthy",
        "config_source": "cloudflare",
        "connected_at": "2026-07-16T10:00:00Z",
        "disconnected_at": None,
    }


async def test_cloudflare_tool_uses_declared_tunnel_and_fixed_endpoint(monkeypatch):
    request = AsyncMock(
        return_value={
            "success": True,
            "result": {"id": TUNNEL_ID, "name": "home", "status": "degraded"},
        }
    )
    monkeypatch.setattr(
        "app.providers.api_ready.tools.get_api_provider_instance",
        lambda instance_id: _cloudflare_instance() if instance_id == "cloudflare_home" else None,
    )
    monkeypatch.setattr(
        "app.providers.api_ready.client.get_provider_secrets",
        lambda provider_id: {"bearer_token": "test-token"},
    )
    monkeypatch.setattr(
        "app.providers.api_ready.client.CloudflareTunnelV1Client.get",
        request,
    )

    result = await health_status("cloudflare_home")

    request.assert_awaited_once_with(
        f"/accounts/0123456789abcdef0123456789abcdef/cfd_tunnel/{TUNNEL_ID}"
    )
    assert result["status"] == "degraded"
    assert result["reported_status"] == "degraded"


async def test_cloudflare_driver_requires_a_bearer_token(monkeypatch):
    monkeypatch.setattr(
        "app.providers.api_ready.tools.get_api_provider_instance",
        lambda instance_id: _cloudflare_instance(),
    )
    monkeypatch.setattr(
        "app.providers.api_ready.client.get_provider_secrets",
        lambda provider_id: {},
    )

    with pytest.raises(ProviderError) as exc_info:
        await health_status("cloudflare_home")

    assert exc_info.value.code == "credentials_missing"
    assert "token" not in exc_info.value.message.lower() or "not configured" in exc_info.value.message


def test_speedtest_normalizer_returns_only_allowlisted_fields():
    result = _normalized_speedtest(
        {
            "measured_at": "2026-07-24T10:00:00Z",
            "download_mbps": 100.5,
            "upload_mbps": 20.25,
            "ping_ms": 12.5,
            "jitter_ms": 1.2,
            "packet_loss_percent": 0.0,
            "server": {
                "id": 123,
                "name": "Example",
                "location": "Rome",
                "country": "Italy",
                "ip": "discarded",
            },
            "isp": "Example ISP",
            "interface_name": "eth0",
            "result_url": "https://www.speedtest.net/result/123",
            "private": "discarded",
        },
        "home_speedtest",
    )

    assert result["instance_id"] == "home_speedtest"
    assert result["download_mbps"] == 100.5
    assert "private" not in result
    assert "ip" not in result["server"]


async def test_speedtest_tool_uses_fixed_run_path_and_bearer_token(monkeypatch):
    request = AsyncMock(
        return_value={
            "measured_at": "2026-07-24T10:00:00Z",
            "download_mbps": 100,
            "upload_mbps": 20,
            "ping_ms": 12,
            "jitter_ms": 1,
            "packet_loss_percent": None,
            "server": {"id": 123, "name": "Example", "location": "Rome", "country": "Italy"},
            "isp": "Example ISP",
            "interface_name": "eth0",
            "result_url": None,
        }
    )
    monkeypatch.setattr(
        "app.providers.api_ready.tools.get_api_provider_instance",
        lambda instance_id: _speedtest_instance(),
    )
    monkeypatch.setattr(
        "app.providers.api_ready.client.get_provider_secrets",
        lambda provider_id: {"bearer_token": "test-token"},
    )
    monkeypatch.setattr(
        "app.providers.api_ready.client.SpeedtestProbeV1Client.post",
        request,
    )

    result = await speedtest_run("home_speedtest")

    request.assert_awaited_once_with("/v1/tests/run")
    assert result["ping_ms"] == 12.0


def test_declared_instance_creates_one_narrow_read_tool(monkeypatch):
    monkeypatch.setattr("app.tools.registry.list_api_provider_instances", lambda: [_instance()])
    from app.tools.registry import get_tool

    tool = get_tool("example_service.health.status")

    assert tool is not None
    assert tool.provider_id == "example_service"
    assert tool.mode == "read"
    assert tool.risk == "low"
    assert tool.input_model.model_json_schema().get("properties") == {}


def test_cloudflare_instance_creates_one_narrow_read_tool(monkeypatch):
    monkeypatch.setattr(
        "app.tools.registry.list_api_provider_instances",
        lambda: [_cloudflare_instance()],
    )
    from app.tools.registry import get_tool

    tool = get_tool("cloudflare_home.tunnel.status")

    assert tool is not None
    assert tool.provider_id == "cloudflare_home"
    assert tool.mode == "read"
    assert tool.risk == "low"
    assert tool.input_model.model_json_schema().get("properties") == {}


def test_speedtest_instance_creates_one_narrow_read_tool(monkeypatch):
    monkeypatch.setattr(
        "app.tools.registry.list_api_provider_instances",
        lambda: [_speedtest_instance()],
    )
    from app.tools.registry import get_tool

    tool = get_tool("home_speedtest.speedtest.run")

    assert tool is not None
    assert tool.provider_id == "home_speedtest"
    assert tool.mode == "read"
    assert tool.risk == "medium"
    assert tool.input_model.model_json_schema().get("properties") == {}
    assert tool.output_model is not None
