import httpx
import pytest

from app.providers.errors import ProviderError
from app.providers.proxmox.client import ProxmoxClient
from app.providers.proxmox.tools import ProxmoxProvider


def _configure(monkeypatch, **extra):
    secrets = {
        "base_url": "https://pve.test:8006",
        "api_token_id": "console@pam!test",
        "api_token_secret": "tok-secret",
        "verify_tls": True,
        **extra,
    }
    monkeypatch.setattr(
        "app.providers.proxmox.client.get_provider_secrets", lambda _pid: secrets, raising=True
    )


def _mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("verify", None)
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.httpclient.httpx.AsyncClient", client_factory)


def test_token_auth_header_format(monkeypatch):
    _configure(monkeypatch)
    client = ProxmoxClient()
    assert client.auth_header() == {"Authorization": "PVEAPIToken=console@pam!test=tok-secret"}


def test_power_token_uses_separate_credentials(monkeypatch):
    _configure(
        monkeypatch,
        power_api_token_id="console-power@pve!homelab-console",
        power_api_token_secret="power-secret",
    )
    client = ProxmoxClient(credential_profile="power")
    assert client.auth_header() == {
        "Authorization": "PVEAPIToken=console-power@pve!homelab-console=power-secret"
    }


async def test_normalized_nodes(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        assert request.headers["authorization"].startswith("PVEAPIToken=")
        return httpx.Response(
            200,
            json={"data": [
                {"node": "pve1", "status": "online", "uptime": 1000, "cpu": 0.12,
                 "maxcpu": 8, "mem": 1024, "maxmem": 4096, "ssl_fingerprint": "AA:BB"},
            ]},
        )

    _mock_transport(monkeypatch, handler)
    from app.providers.proxmox.tools import nodes_list

    result = await nodes_list()
    assert result["nodes"] == [
        {
            "node": "pve1",
            "status": "online",
            "uptime_seconds": 1000,
            "cpu_usage": 0.12,
            "max_cpu": 8,
            "memory_used_bytes": 1024,
            "memory_total_bytes": 4096,
        }
    ]
    # Raw vendor fields must not leak.
    assert "ssl_fingerprint" not in str(result)


async def test_normalized_guests(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        return httpx.Response(
            200,
            json={"data": [
                {"type": "qemu", "vmid": 100, "name": "vm-a", "status": "running", "node": "pve1"},
                {"type": "lxc", "vmid": 200, "name": "ct-b", "status": "stopped", "node": "pve1"},
                {"type": "storage", "storage": "local", "node": "pve1", "maxdisk": 10, "disk": 5},
            ]},
        )

    _mock_transport(monkeypatch, handler)
    from app.providers.proxmox.tools import guests_list

    result = await guests_list()
    assert [guest["vmid"] for guest in result["guests"]] == [100, 200]
    assert result["guests"][0]["guest_type"] == "qemu"
    assert result["guests"][1]["guest_type"] == "lxc"

    only_vms = await guests_list(guest_type="qemu")
    assert [guest["vmid"] for guest in only_vms["guests"]] == [100]


async def test_topology_snapshot_is_normalized_and_narrow(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        if request.url.path.endswith("/cluster/status"):
            data = [
                {"id": "cluster", "name": "lab", "type": "cluster", "quorate": 1},
                {"id": "node/pve1", "name": "pve1", "type": "node", "online": 1},
            ]
        elif request.url.path.endswith("/nodes"):
            data = [{"node": "pve1", "status": "online", "cpu": 0.1, "secret": "drop-me"}]
        else:
            data = [
                {"type": "lxc", "vmid": 103, "name": "adguard", "status": "running", "node": "pve1", "token": "drop-me"},
                {"type": "storage", "storage": "local", "node": "pve1"},
            ]
        return httpx.Response(200, json={"data": data})

    _mock_transport(monkeypatch, handler)
    from app.providers.proxmox.tools import topology_snapshot

    result = await topology_snapshot()
    assert result["entries"][0]["quorate"] is True
    assert result["nodes"][0]["node"] == "pve1"
    assert result["guests"][0]["vmid"] == 103
    assert "drop-me" not in str(result)


async def test_timeout_maps_to_provider_error(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        raise httpx.ConnectTimeout("timeout")

    _mock_transport(monkeypatch, handler)
    with pytest.raises(ProviderError) as exc_info:
        await ProxmoxClient().get("/api2/json/version")
    assert exc_info.value.code == "timeout"


async def test_auth_error(monkeypatch):
    _configure(monkeypatch)
    _mock_transport(monkeypatch, lambda request: httpx.Response(401, json={}))
    with pytest.raises(ProviderError) as exc_info:
        await ProxmoxClient().get("/api2/json/version")
    assert exc_info.value.code == "auth_failed"
    # The token must never appear in the error.
    assert "tok-secret" not in str(exc_info.value)


async def test_unconfigured_provider_is_unavailable():
    health = await ProxmoxProvider().health()
    assert health.status == "unavailable"


async def test_unreachable_provider_health(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        raise httpx.ConnectError("no route to host")

    _mock_transport(monkeypatch, handler)
    health = await ProxmoxProvider().health()
    assert health.status == "unreachable"


async def test_disk_temperatures_urlencodes_devpath(monkeypatch):
    _configure(monkeypatch)
    seen_paths = []
    seen_queries = []

    def handler(request):
        seen_paths.append(request.url.path)
        seen_queries.append(request.url.query.decode())
        if request.url.path == "/api2/json/nodes":
            data = [{"node": "pve", "status": "online"}]
        elif request.url.path == "/api2/json/nodes/pve/disks/list":
            data = [{"devpath": "/dev/disk/by-id/ata-PNY_CS900", "model": "PNY", "type": "ssd"}]
        else:
            assert request.url.path == "/api2/json/nodes/pve/disks/smart"
            assert request.url.params.get("disk") == "/dev/disk/by-id/ata-PNY_CS900"
            data = {"attributes": [{"id": "194", "raw": "33"}]}
        return httpx.Response(200, json={"data": data})

    _mock_transport(monkeypatch, handler)
    from app.providers.proxmox.tools import disks_temperatures

    result = await disks_temperatures()
    assert result["maximum_temperature_c"] == 33
    assert "disk=%2Fdev%2Fdisk%2Fby-id%2Fata-PNY_CS900" in seen_queries
    assert "/api2/json/nodes/pve/disks/smart" in seen_paths


async def test_tls_disabled_forbidden_in_live(monkeypatch):
    _configure(monkeypatch, verify_tls=False)
    monkeypatch.setenv("APP_ENV", "live")
    from app.core.settings import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(ProviderError) as exc_info:
            await ProxmoxClient().get("/api2/json/version")
        assert exc_info.value.code == "configuration_missing"
    finally:
        monkeypatch.setenv("APP_ENV", "test")
        get_settings.cache_clear()
