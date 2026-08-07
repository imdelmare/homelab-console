"""Tests for the Uptime Kuma and EMQX providers."""

import httpx
import pytest

from app.providers.emqx.tools import EmqxProvider
from app.providers.errors import ProviderError
from app.providers.uptimekuma.tools import UptimeKumaProvider
from app.tools.summaries import _owned_by_direct_watcher

METRICS_BODY = """\
# HELP monitor_status Monitor Status (1 = UP, 0= DOWN, 2= PENDING, 3= MAINTENANCE)
# TYPE monitor_status gauge
monitor_status{monitor_name="proxmox",monitor_type="http",monitor_url="https://pve.lab"} 1
monitor_status{monitor_name="frigate",monitor_type="http",monitor_url="https://frg.lab"} 0
monitor_status{monitor_name="backup",monitor_type="ping",monitor_url="https://",monitor_hostname="10.0.0.9",monitor_port="null"} 3
monitor_status{monitor_name="dns",monitor_type="dns",monitor_url="https://",monitor_hostname="example.com",monitor_port="53"} 1
monitor_response_time{monitor_name="proxmox"} 42
"""


@pytest.mark.parametrize(
    ("provider_id", "code", "owned"),
    [
        ("uptimekuma", "monitors_down", True),
        ("nutups", "battery_low", True),
        ("cloudflaretunnel", "tunnel_unavailable", True),
        ("opnsense", "gateway_offline", True),
        ("opnsense", "wireguard_stale", True),
        ("opnsense", "firmware_updates", False),
        ("homeassistant", "problem_entities", False),
    ],
)
def test_lab_alert_ownership_excludes_only_direct_watcher_findings(
    provider_id, code, owned
):
    assert _owned_by_direct_watcher({"provider_id": provider_id, "code": code}) is owned


def _configure(monkeypatch, provider_id: str, **extra):
    secrets = {
        "base_url": f"http://{provider_id}.test",
        "api_key": "test-key",
        "api_secret": "test-secret",
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


@pytest.mark.parametrize("provider", [UptimeKumaProvider(), EmqxProvider()], ids=lambda p: p.id)
async def test_unconfigured_reports_unavailable(provider):
    health = await provider.health()
    assert health.status == "unavailable"


async def test_uptimekuma_monitors_parsed_from_metrics(monkeypatch):
    _configure(monkeypatch, "uptimekuma")

    def handler(request):
        assert request.url.path == "/metrics"
        assert request.headers.get("authorization", "").startswith("Basic ")
        return httpx.Response(200, text=METRICS_BODY, headers={"content-type": "text/plain"})

    _mock_transport(monkeypatch, handler)
    from app.providers.uptimekuma.tools import monitors_status

    result = await monitors_status()
    assert result["total"] == 4
    assert result["by_status"] == {"up": 2, "down": 1, "maintenance": 1}
    names = {monitor["name"]: monitor["status"] for monitor in result["monitors"]}
    assert names == {"proxmox": "up", "frigate": "down", "backup": "maintenance", "dns": "up"}
    targets = {monitor["name"]: monitor["target"] for monitor in result["monitors"]}
    assert targets["frigate"] == "https://frg.lab"
    assert targets["backup"] == "10.0.0.9"
    assert targets["dns"] == "example.com:53"


async def test_uptimekuma_requires_api_key_for_metrics(monkeypatch):
    _configure(monkeypatch, "uptimekuma", api_key="")
    from app.providers.uptimekuma.tools import monitors_status

    with pytest.raises(ProviderError) as exc_info:
        await monitors_status()
    assert exc_info.value.code == "credentials_missing"


async def test_uptimekuma_summary_degrades_when_monitors_are_down(monkeypatch):
    _configure(monkeypatch, "uptimekuma")

    def handler(request):
        assert request.url.path == "/metrics"
        return httpx.Response(200, text=METRICS_BODY, headers={"content-type": "text/plain"})

    _mock_transport(monkeypatch, handler)
    from app.tools.summaries import uptimekuma_summary

    result = await uptimekuma_summary()
    summary = result["summary"]
    assert summary["status"] == "degraded"
    assert summary["severity"] == "critical"
    assert summary["metrics"]["monitors_down"] == 1
    assert summary["metrics"]["down_monitors"] == [
        {"name": "frigate", "type": "http", "target": "https://frg.lab", "status": "down"}
    ]
    assert "frigate" in summary["findings"][0]["message"]
    assert "frigate" in summary["next_actions"][0]


async def test_uptimekuma_statuspage_heartbeat(monkeypatch):
    _configure(monkeypatch, "uptimekuma")

    def handler(request):
        assert request.url.path == "/api/status-page/heartbeat/lab"
        return httpx.Response(200, json={
            "heartbeatList": {
                "7": [{"status": 1, "ping": 12, "time": "2026-07-13 10:00:00"}],
                "9": [{"status": 0, "ping": None, "time": "2026-07-13 10:00:00"}],
            },
            "uptimeList": {"7_24": 0.999, "9_24": 0.5},
        })

    _mock_transport(monkeypatch, handler)
    from app.providers.uptimekuma.tools import statuspage_heartbeat

    result = await statuspage_heartbeat("lab")
    assert result["total"] == 2
    by_id = {monitor["monitor_id"]: monitor for monitor in result["monitors"]}
    assert by_id["7"]["status"] == "up"
    assert by_id["7"]["uptime_24h"] == 0.999
    assert by_id["9"]["status"] == "down"


async def test_fritzbox_summary_ignores_guest_wifi_disabled_index(monkeypatch):
    async def fake_health(provider_id):
        return {"status": "healthy", "detail": ""}

    async def fake_device_info(provider_id):
        return {"device": {"model": "FRITZ!Box 4040"}}

    async def fake_wan_status(provider_id):
        return {"available": True, "wan": {"physical_link_status": "Up"}}

    async def fake_wifi_summary(provider_id):
        return {
            "wifi": [
                {"index": 1, "enabled": True},
                {"index": 2, "enabled": False},
                {"index": 3, "enabled": False},
            ]
        }

    monkeypatch.setattr("app.tools.summaries._health", fake_health)
    monkeypatch.setattr("app.providers.fritzbox.tools.device_info", fake_device_info)
    monkeypatch.setattr("app.providers.fritzbox.tools.wan_status", fake_wan_status)
    monkeypatch.setattr("app.providers.fritzbox.tools.wifi_summary", fake_wifi_summary)

    from app.tools.summaries import fritzbox_primary_summary

    result = await fritzbox_primary_summary()

    assert result["summary"]["metrics"]["wifi_radios_disabled"] == 2
    assert result["summary"]["metrics"]["wifi_radios_disabled_actionable"] == 1
    assert result["summary"]["metrics"]["wifi_radios_disabled_ignored"] == 1
    assert result["summary"]["findings"][0]["message"] == "1 disabled Wi-Fi radio(s), indexes: 2"


async def test_fritzbox_summary_guest_wifi_only_is_healthy(monkeypatch):
    async def fake_health(provider_id):
        return {"status": "healthy", "detail": ""}

    async def fake_device_info(provider_id):
        return {"device": {"model": "FRITZ!Box 4040"}}

    async def fake_wan_status(provider_id):
        return {"available": True, "wan": {"physical_link_status": "Up"}}

    async def fake_wifi_summary(provider_id):
        return {
            "wifi": [
                {"index": 1, "enabled": True},
                {"index": 2, "enabled": True},
                {"index": 3, "enabled": False},
            ]
        }

    monkeypatch.setattr("app.tools.summaries._health", fake_health)
    monkeypatch.setattr("app.providers.fritzbox.tools.device_info", fake_device_info)
    monkeypatch.setattr("app.providers.fritzbox.tools.wan_status", fake_wan_status)
    monkeypatch.setattr("app.providers.fritzbox.tools.wifi_summary", fake_wifi_summary)

    from app.tools.summaries import fritzbox_primary_summary

    result = await fritzbox_primary_summary()

    assert result["summary"]["status"] == "healthy"
    assert result["summary"]["metrics"]["wifi_radios_disabled"] == 1
    assert result["summary"]["metrics"]["wifi_radios_disabled_actionable"] == 0
    assert result["summary"]["findings"] == []


async def test_fritzbox_secondary_missing_wan_is_not_a_warning(monkeypatch):
    async def fake_health(provider_id):
        return {"status": "healthy", "detail": ""}

    async def fake_device_info(provider_id):
        return {"device": {"model": "FRITZ!WLAN Repeater 1750E"}}

    async def fake_wan_status(provider_id):
        return {"available": False}

    async def fake_wifi_summary(provider_id):
        return {"wifi": [{"index": 1, "enabled": True}]}

    monkeypatch.setattr("app.tools.summaries._health", fake_health)
    monkeypatch.setattr("app.providers.fritzbox.tools.device_info", fake_device_info)
    monkeypatch.setattr("app.providers.fritzbox.tools.wan_status", fake_wan_status)
    monkeypatch.setattr("app.providers.fritzbox.tools.wifi_summary", fake_wifi_summary)

    from app.tools.summaries import fritzbox_secondary_summary

    result = await fritzbox_secondary_summary()

    assert result["summary"]["metrics"]["wan_available"] is False
    assert result["summary"]["findings"] == []


async def test_emqx_nodes_normalized(monkeypatch):
    _configure(monkeypatch, "emqx")

    def handler(request):
        assert request.url.path == "/api/v5/nodes"
        return httpx.Response(200, json=[{
            "node": "emqx@127.0.0.1", "node_status": "running", "version": "5.6.0",
            "uptime": 123456, "connections": 12, "memory_used": 100, "memory_total": 200,
            "load1": 0.2, "otp_release": "26/14.2",
        }])

    _mock_transport(monkeypatch, handler)
    from app.providers.emqx.tools import nodes_list

    result = await nodes_list()
    assert result["total"] == 1
    node = result["nodes"][0]
    assert node["status"] == "running"
    assert node["connections"] == 12
    assert "otp_release" not in node


async def test_emqx_stats_merged(monkeypatch):
    _configure(monkeypatch, "emqx")

    def handler(request):
        return httpx.Response(200, json=[
            {"connections.count": 5, "subscriptions.count": 9, "topics.count": 4},
            {"connections.count": 3, "subscriptions.count": 1, "topics.count": 2},
        ])

    _mock_transport(monkeypatch, handler)
    from app.providers.emqx.tools import stats

    result = await stats()
    assert result["nodes_reporting"] == 2
    assert result["stats"]["connections"] == 8
    assert result["stats"]["subscriptions"] == 10
    assert result["stats"]["topics"] == 6


async def test_emqx_stopped_node_degraded(monkeypatch):
    _configure(monkeypatch, "emqx")
    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=[
            {"node": "a", "node_status": "running"},
            {"node": "b", "node_status": "stopped"},
        ]),
    )
    health = await EmqxProvider().health()
    assert health.status == "degraded"


async def test_emqx_auth_error(monkeypatch):
    _configure(monkeypatch, "emqx")
    _mock_transport(monkeypatch, lambda request: httpx.Response(401, json={}))
    from app.providers.emqx.client import EmqxClient

    with pytest.raises(ProviderError) as exc_info:
        await EmqxClient().get("/api/v5/nodes")
    assert exc_info.value.code == "auth_failed"
    assert "test-secret" not in str(exc_info.value)
