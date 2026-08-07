"""Tests for the HTTP providers built on the shared BaseJsonClient
(AdGuard, Nextcloud, MikroTik) plus health-status mapping for all providers."""

import httpx
import pytest

from app.providers.adguard.tools import AdguardProvider
from app.providers.asterisk.tools import AsteriskProvider
from app.providers.errors import ProviderError
from app.providers.frigate.tools import FrigateProvider
from app.providers.fritzbox.tools import FritzBoxProvider
from app.providers.homeassistant.tools import HomeAssistantProvider
from app.providers.httpclient import BaseJsonClient
from app.providers.mikrotik.tools import MikrotikProvider
from app.providers.nextcloud.tools import NextcloudProvider
from app.providers.opnsense.tools import OpnsenseProvider
from app.providers.proxmox.tools import ProxmoxProvider


def _configure(monkeypatch, provider_id: str, **extra):
    secrets = {
        "base_url": f"https://{provider_id}.test",
        "username": "operator",
        "password": "pw",
        "app_password": "pw",
        "verify_tls": True,
        **extra,
    }
    monkeypatch.setattr(
        f"app.providers.{provider_id}.client.get_provider_secrets",
        lambda _pid: secrets,
        raising=True,
    )


def _mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("verify", None)
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.httpclient.httpx.AsyncClient", client_factory)


@pytest.mark.parametrize(
    "provider",
    [
        ProxmoxProvider(),
        OpnsenseProvider(),
        HomeAssistantProvider(),
        FrigateProvider(),
        AdguardProvider(),
        NextcloudProvider(),
        MikrotikProvider(),
        FritzBoxProvider("fritzbox_primary"),
        FritzBoxProvider("fritzbox_secondary"),
        AsteriskProvider(),
    ],
    ids=lambda provider: provider.id,
)
async def test_unconfigured_provider_reports_unavailable(provider):
    health = await provider.health()
    assert health.status == "unavailable"
    assert health.provider_id == provider.id


async def test_adguard_status_normalized(monkeypatch):
    _configure(monkeypatch, "adguard")

    def handler(request):
        assert request.headers.get("authorization", "").startswith("Basic ")
        return httpx.Response(200, json={
            "version": "v0.107.50", "running": True, "protection_enabled": True,
            "dns_addresses": ["10.0.0.3"], "dns_port": 53,
            "http_port": 80, "language": "en",  # extra vendor fields
        })

    _mock_transport(monkeypatch, handler)
    from app.providers.adguard.tools import status

    result = await status()
    assert result["status"]["version"] == "v0.107.50"
    assert result["status"]["protection_enabled"] is True
    assert "http_port" not in result["status"]


async def test_base_json_client_requires_explicit_text_mode(monkeypatch):
    client = BaseJsonClient()
    client.provider_id = "test"
    client.base_url = "https://test.invalid"

    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            text="plain diagnostics",
            headers={"content-type": "text/plain"},
        ),
    )

    with pytest.raises(ProviderError) as exc_info:
        await client.get("/diagnostics")
    assert exc_info.value.code == "invalid_response"
    assert await client.get("/diagnostics", response_mode="text") == "plain diagnostics"
    assert await client.get("/diagnostics", response_mode="auto") == "plain diagnostics"


@pytest.mark.parametrize(
    ("provider", "provider_id", "credentials"),
    [
        (HomeAssistantProvider(), "homeassistant", {"token": "test-token"}),
        (FrigateProvider(), "frigate", {}),
    ],
)
async def test_migrated_http_provider_maps_tls_error_to_misconfigured(
    monkeypatch, provider, provider_id, credentials
):
    _configure(monkeypatch, provider_id, **credentials)

    async def tls_failure(*_args, **_kwargs):
        raise ProviderError("tls_error", "TLS handshake failed")

    monkeypatch.setattr(f"app.providers.{provider_id}.client.{provider.client().__class__.__name__}.get", tls_failure)
    health = await provider.health()
    assert health.status == "misconfigured"


async def test_adguard_stats_top_lists_flattened(monkeypatch):
    _configure(monkeypatch, "adguard")

    def handler(request):
        return httpx.Response(200, json={
            "num_dns_queries": 1000, "num_blocked_filtering": 100,
            "avg_processing_time": 0.002, "time_units": "days",
            "top_queried_domains": [{"example.com": 42}, {"lab.local": 7}],
            "top_blocked_domains": [{"ads.example": 33}],
        })

    _mock_transport(monkeypatch, handler)
    from app.providers.adguard.tools import stats

    result = await stats()
    assert result["stats"]["dns_queries"] == 1000
    assert result["stats"]["top_queried_domains"][0] == {"name": "example.com", "count": 42}
    assert result["stats"]["top_blocked_domains"] == [{"name": "ads.example", "count": 33}]


async def test_nextcloud_status_and_capabilities(monkeypatch):
    _configure(monkeypatch, "nextcloud")

    def handler(request):
        if request.url.path == "/status.php":
            return httpx.Response(200, json={
                "installed": True, "maintenance": False, "needsDbUpgrade": False,
                "versionstring": "29.0.1", "edition": "",
            })
        assert request.headers.get("ocs-apirequest") == "true"
        return httpx.Response(200, json={
            "ocs": {"data": {
                "version": {"string": "29.0.1", "edition": ""},
                "capabilities": {"files": {}, "theming": {}, "user_status": {}},
            }}
        })

    _mock_transport(monkeypatch, handler)
    from app.providers.nextcloud.tools import capabilities, status

    assert (await status())["status"]["version"] == "29.0.1"
    caps = (await capabilities())["capabilities"]
    assert caps["apps"] == ["files", "theming", "user_status"]


async def test_nextcloud_maintenance_reports_degraded(monkeypatch):
    _configure(monkeypatch, "nextcloud")
    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"installed": True, "maintenance": True}),
    )
    health = await NextcloudProvider().health()
    assert health.status == "degraded"


async def test_mikrotik_resource_normalized(monkeypatch):
    _configure(monkeypatch, "mikrotik")

    def handler(request):
        assert request.url.path == "/rest/system/resource"
        return httpx.Response(200, json={
            "version": "7.15.2 (stable)", "board-name": "hAP ac3",
            "architecture-name": "arm", "uptime": "2w3d", "cpu-load": 4,
            "free-memory": 100, "total-memory": 200,
            "free-hdd-space": 50, "total-hdd-space": 128,
        })

    _mock_transport(monkeypatch, handler)
    from app.providers.mikrotik.tools import system_resource

    resource = (await system_resource())["resource"]
    assert resource["version"] == "7.15.2 (stable)"
    assert resource["board_name"] == "hAP ac3"
    assert resource["cpu_load_percent"] == 4


async def test_mikrotik_system_health_normalized(monkeypatch):
    _configure(monkeypatch, "mikrotik")

    def handler(request):
        assert request.url.path == "/rest/system/health"
        return httpx.Response(200, json=[
            {"name": "temperature", "value": "41", "type": "C"},
            {"name": "voltage", "value": "24.2", "type": "V"},
        ])

    _mock_transport(monkeypatch, handler)
    from app.providers.mikrotik.tools import system_health

    health = (await system_health())["health"]
    assert health["maximum_temperature_c"] == 41
    assert health["voltage_v"] == 24.2
    assert health["temperature_sensors"][0]["sensor_id"] == "temperature"


async def test_mikrotik_interfaces_normalized(monkeypatch):
    _configure(monkeypatch, "mikrotik")

    def handler(request):
        return httpx.Response(200, json=[
            {"name": "ether1", "type": "ether", "running": "true", "disabled": "false",
             "mac-address": "AA:BB:CC:00:11:22", "rx-byte": "123", "tx-byte": "456"},
            {"name": "lte1", "type": "lte", "running": "false", "disabled": "false"},
        ])

    _mock_transport(monkeypatch, handler)
    from app.providers.mikrotik.tools import interfaces_list

    result = await interfaces_list()
    assert result["total"] == 2
    assert result["interfaces"][0]["running"] is True
    assert result["interfaces"][1]["running"] is False


async def test_mikrotik_lte_absent_is_graceful(monkeypatch):
    _configure(monkeypatch, "mikrotik")
    _mock_transport(monkeypatch, lambda request: httpx.Response(404, json={}))
    from app.providers.mikrotik.tools import lte_status

    result = await lte_status()
    assert result["total"] == 0
    assert "no LTE interface" in result["note"]


def _soap_response(values: dict[str, object]) -> str:
    body = "".join(f"<New{key}>{value}</New{key}>" for key, value in values.items())
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<s:Body><u:GetInfoResponse xmlns:u="urn:test">{body}</u:GetInfoResponse></s:Body>'
        "</s:Envelope>"
    )


def _tr64_description() -> str:
    return (
        '<?xml version="1.0"?>'
        '<root xmlns="urn:dslforum-org:device-1-0">'
        "<device><serviceList>"
        "<service><serviceType>urn:dslforum-org:service:DeviceInfo:1</serviceType><controlURL>/upnp/control/deviceinfo</controlURL></service>"
        "<service><serviceType>urn:dslforum-org:service:WANCommonInterfaceConfig:1</serviceType><controlURL>/upnp/control/wancommonifconfig1</controlURL></service>"
        "<service><serviceType>urn:dslforum-org:service:WLANConfiguration:1</serviceType><controlURL>/upnp/control/wlanconfig1</controlURL></service>"
        "</serviceList></device></root>"
    )


async def test_fritzbox_device_info_normalized(monkeypatch):
    _configure(monkeypatch, "fritzbox")
    monkeypatch.setattr(
        "app.providers.fritzbox.client.get_provider_secrets",
        lambda _pid: {"base_url": "http://fritzbox.test:49000", "username": "operator", "password": "pw"},
        raising=True,
    )

    def handler(request):
        if request.url.path == "/tr64desc.xml":
            return httpx.Response(200, text=_tr64_description())
        assert request.url.path == "/upnp/control/deviceinfo"
        assert "DeviceInfo:1#GetInfo" in request.headers.get("soapaction", "")
        return httpx.Response(200, text=_soap_response({
            "ManufacturerName": "AVM",
            "ModelName": "FRITZ!Box 7590",
            "SerialNumber": "abc",
            "SoftwareVersion": "8.00",
            "HardwareVersion": "123",
            "UpTime": "42",
        }))

    _mock_transport(monkeypatch, handler)
    from app.providers.fritzbox.tools import device_info

    result = await device_info("fritzbox_primary")
    assert result["device"]["manufacturer"] == "AVM"
    assert result["device"]["model"] == "FRITZ!Box 7590"
    assert result["device"]["uptime_seconds"] == 42


async def test_fritzbox_wan_status_normalized(monkeypatch):
    monkeypatch.setattr(
        "app.providers.fritzbox.client.get_provider_secrets",
        lambda _pid: {"base_url": "http://fritzbox.test:49000", "username": "operator", "password": "pw"},
        raising=True,
    )

    def handler(request):
        if request.url.path == "/tr64desc.xml":
            return httpx.Response(200, text=_tr64_description())
        action = request.headers.get("soapaction", "")
        if "GetCommonLinkProperties" in action:
            return httpx.Response(200, text=_soap_response({
                "WANAccessType": "DSL",
                "PhysicalLinkStatus": "Up",
                "Layer1UpstreamMaxBitRate": "50000000",
                "Layer1DownstreamMaxBitRate": "200000000",
            }))
        if "GetTotalBytesSent" in action:
            return httpx.Response(200, text=_soap_response({"TotalBytesSent": "100"}))
        return httpx.Response(200, text=_soap_response({"TotalBytesReceived": "250"}))

    _mock_transport(monkeypatch, handler)
    from app.providers.fritzbox.tools import wan_status

    result = await wan_status("fritzbox_primary")
    assert result["wan"]["physical_link_status"] == "Up"
    assert result["wan"]["upstream_max_mbps"] == 50
    assert result["wan"]["downstream_max_mbps"] == 200
    assert result["wan"]["bytes_sent"] == 100
    assert result["wan"]["bytes_received"] == 250


async def test_fritzbox_wan_absent_is_graceful(monkeypatch):
    monkeypatch.setattr(
        "app.providers.fritzbox.client.get_provider_secrets",
        lambda _pid: {"base_url": "http://fritzbox.test:49000", "username": "operator", "password": "pw"},
        raising=True,
    )

    def handler(request):
        if request.url.path == "/tr64desc.xml":
            return httpx.Response(200, text=(
                '<?xml version="1.0"?><root xmlns="urn:dslforum-org:device-1-0">'
                "<device><serviceList>"
                "<service><serviceType>urn:dslforum-org:service:DeviceInfo:1</serviceType><controlURL>/upnp/control/deviceinfo</controlURL></service>"
                "</serviceList></device></root>"
            ))
        return httpx.Response(500, text="")

    _mock_transport(monkeypatch, handler)
    from app.providers.fritzbox.tools import wan_status

    result = await wan_status("fritzbox_secondary")
    assert result["available"] is False
    assert result["wan"] == {}


async def test_fritzbox_wifi_summary_skips_missing_radios(monkeypatch):
    monkeypatch.setattr(
        "app.providers.fritzbox.client.get_provider_secrets",
        lambda _pid: {"base_url": "http://fritzbox.test:49000", "username": "operator", "password": "pw"},
        raising=True,
    )

    def handler(request):
        if request.url.path == "/tr64desc.xml":
            return httpx.Response(200, text=_tr64_description())
        if request.url.path.endswith("wlanconfig1"):
            return httpx.Response(200, text=_soap_response({
                "Enable": "1", "Status": "Up", "SSID": "lab", "Channel": "6", "Standard": "n",
            }))
        return httpx.Response(404, text="")

    _mock_transport(monkeypatch, handler)
    from app.providers.fritzbox.tools import wifi_summary

    result = await wifi_summary("fritzbox_primary")
    assert result["total"] == 1
    assert result["wifi"][0]["enabled"] is True
    assert result["wifi"][0]["ssid"] == "lab"


@pytest.mark.parametrize("provider_id", ["adguard", "nextcloud", "mikrotik"])
async def test_http_auth_error_mapping(monkeypatch, provider_id):
    _configure(monkeypatch, provider_id)
    _mock_transport(monkeypatch, lambda request: httpx.Response(401, json={}))

    module = __import__(f"app.providers.{provider_id}.client", fromlist=["*"])
    client_class = {
        "adguard": "AdguardClient", "nextcloud": "NextcloudClient", "mikrotik": "MikrotikClient",
    }[provider_id]
    client = getattr(module, client_class)()

    with pytest.raises(ProviderError) as exc_info:
        await client.get("/any-read-path")
    assert exc_info.value.code == "auth_failed"
    assert "pw" not in str(exc_info.value)


async def test_fritzbox_auth_error_mapping(monkeypatch):
    monkeypatch.setattr(
        "app.providers.fritzbox.client.get_provider_secrets",
        lambda _pid: {"base_url": "http://fritzbox.test:49000", "username": "operator", "password": "pw"},
        raising=True,
    )
    _mock_transport(monkeypatch, lambda request: httpx.Response(401, text=""))
    from app.providers.fritzbox.client import FritzBoxClient

    with pytest.raises(ProviderError) as exc_info:
        await FritzBoxClient("fritzbox_primary").get_description()
    assert exc_info.value.code == "auth_failed"
    assert "pw" not in str(exc_info.value)


async def test_fritzbox_web_login_invalid_xml_maps_to_provider_error(monkeypatch):
    monkeypatch.setattr(
        "app.providers.fritzbox.client.get_provider_secrets",
        lambda _pid: {"base_url": "http://fritzbox.test:49000", "username": "operator", "password": "pw"},
        raising=True,
    )

    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<not-xml"))
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.fritzbox.client.httpx.AsyncClient", client_factory)
    from app.providers.fritzbox.tools import system_temperature

    with pytest.raises(ProviderError) as exc_info:
        await system_temperature("fritzbox_secondary")
    assert exc_info.value.code == "invalid_response"
    assert "pw" not in str(exc_info.value)


@pytest.mark.parametrize("provider_id", ["adguard", "nextcloud", "mikrotik"])
async def test_http_timeout_mapping(monkeypatch, provider_id):
    _configure(monkeypatch, provider_id)

    def handler(request):
        raise httpx.ConnectTimeout("timeout")

    _mock_transport(monkeypatch, handler)
    module = __import__(f"app.providers.{provider_id}.client", fromlist=["*"])
    client_class = {
        "adguard": "AdguardClient", "nextcloud": "NextcloudClient", "mikrotik": "MikrotikClient",
    }[provider_id]

    with pytest.raises(ProviderError) as exc_info:
        await getattr(module, client_class)().get("/any-read-path")
    assert exc_info.value.code == "timeout"


async def test_shared_client_tls_disabled_forbidden_in_live(monkeypatch):
    _configure(monkeypatch, "mikrotik", verify_tls=False)
    monkeypatch.setenv("APP_ENV", "live")
    from app.core.settings import get_settings
    from app.providers.mikrotik.client import MikrotikClient

    get_settings.cache_clear()
    try:
        with pytest.raises(ProviderError) as exc_info:
            await MikrotikClient().get("/rest/system/resource")
        assert exc_info.value.code == "configuration_missing"
    finally:
        monkeypatch.setenv("APP_ENV", "test")
        get_settings.cache_clear()
