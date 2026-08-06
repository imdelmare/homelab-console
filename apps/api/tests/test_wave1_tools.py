"""Wave 1 tools: node status, backups, WireGuard, services, lab.overview."""

import time
from typing import cast

import httpx
import pytest

from app.providers.base import ProviderHealth, ProviderStatusValue
from app.providers.errors import ProviderError


def _configure_proxmox(monkeypatch):
    secrets = {
        "base_url": "https://pve.test:8006",
        "api_token_id": "console@pam!test",
        "api_token_secret": "tok",
        "verify_tls": True,
    }
    monkeypatch.setattr(
        "app.providers.proxmox.client.get_provider_secrets", lambda _pid: secrets, raising=True
    )


def _configure_opnsense(monkeypatch):
    secrets = {"base_url": "https://opn.test", "api_key": "k", "api_secret": "s", "verify_tls": True}
    monkeypatch.setattr(
        "app.providers.opnsense.client.get_provider_secrets", lambda _pid: secrets, raising=True
    )


def _base_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("verify", None)
        return real(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.httpclient.httpx.AsyncClient", factory)


async def test_proxmox_node_status_normalized(monkeypatch):
    _configure_proxmox(monkeypatch)

    def handler(request):
        assert request.url.path == "/api2/json/nodes/pve1/status"
        return httpx.Response(200, json={"data": {
            "uptime": 360000, "cpu": 0.07, "loadavg": ["0.5", "0.4", "0.3"],
            "kversion": "Linux 6.8.12-1-pve",
            "cpuinfo": {"cpus": 8, "model": "Intel(R) N100"},
            "memory": {"total": 16000, "used": 9000, "free": 7000},
            "swap": {"total": 8000, "used": 100},
            "rootfs": {"total": 100000, "used": 30000, "avail": 70000},
            "pveversion": "pve-manager/8.2",  # extra, dropped
        }})

    _base_transport(monkeypatch, handler)
    from app.providers.proxmox.tools import node_status

    status = (await node_status("pve1"))["node_status"]
    assert status["node"] == "pve1"
    assert status["loadavg"] == [0.5, 0.4, 0.3]
    assert status["cpu_count"] == 8
    assert status["memory_used_bytes"] == 9000
    assert status["rootfs_total_bytes"] == 100000
    assert "pveversion" not in status


async def test_proxmox_backups_aggregated(monkeypatch):
    _configure_proxmox(monkeypatch)
    now = int(time.time())

    def handler(request):
        path = request.url.path
        if path == "/api2/json/nodes":
            return httpx.Response(200, json={"data": [
                {"node": "pve1", "status": "online"},
                {"node": "pve2", "status": "offline"},
            ]})
        if path == "/api2/json/nodes/pve1/storage":
            return httpx.Response(200, json={"data": [
                {"storage": "local", "content": "backup,iso", "active": 1, "shared": 0},
                {"storage": "images", "content": "images", "active": 1},
                {"storage": "inactive-bk", "content": "backup", "active": 0},
            ]})
        if path == "/api2/json/nodes/pve1/storage/local/content":
            return httpx.Response(200, json={"data": [
                {"volid": "local:backup/vzdump-qemu-100-a.vma.zst", "vmid": 100,
                 "ctime": now - 86400 * 2, "size": 111},
                {"volid": "local:backup/vzdump-qemu-100-b.vma.zst", "vmid": 100,
                 "ctime": now - 3600, "size": 222},
                {"volid": "local:backup/vzdump-lxc-200-a.tar.zst", "vmid": 200,
                 "ctime": now - 86400 * 10, "size": 333},
            ]})
        raise AssertionError(f"unexpected path {path}")

    _base_transport(monkeypatch, handler)
    from app.providers.proxmox.tools import backups_list

    result = await backups_list()
    assert result["guests_with_backups"] == 2
    assert result["total_backups"] == 3
    by_vmid = {item["vmid"]: item for item in result["backups_by_guest"]}
    assert by_vmid[100]["backups_count"] == 2
    assert by_vmid[100]["latest_size_bytes"] == 222  # newest wins
    assert by_vmid[100]["latest_age_days"] < 1
    assert by_vmid[200]["latest_age_days"] >= 9.9


async def test_wireguard_status_normalized(monkeypatch):
    _configure_opnsense(monkeypatch)
    now = int(time.time())

    def handler(request):
        assert request.url.path == "/api/wireguard/service/show"
        return httpx.Response(200, json={"rows": [
            {"type": "interface", "if": "wg0", "name": "vps-link", "port": "51820",
             "public-key": "IFACEPUBKEY="},
            {"type": "peer", "if": "wg0", "name": "vps", "endpoint": "203.0.113.7:51820",
             "public-key": "PEERPUBKEY=", "latest-handshake": now - 30,
             "transfer-rx": 1000, "transfer-tx": 2000, "allowed-ips": "10.6.0.2/32"},
            {"type": "peer", "if": "wg0", "name": "phone", "endpoint": "(none)",
             "latest-handshake": now - 7200, "transfer-rx": 0, "transfer-tx": 0,
             "allowed-ips": "10.6.0.3/32"},
        ]})

    _base_transport(monkeypatch, handler)
    from app.providers.opnsense.tools import wireguard_status

    result = await wireguard_status()
    assert result["peers_total"] == 2
    assert result["peers_connected"] == 1
    assert result["peers_stale"] == ["phone"]
    interface = result["interfaces"][0]
    assert interface["device"] == "wg0"
    peers = {peer["name"]: peer for peer in interface["peers"]}
    assert peers["vps"]["connected"] is True
    assert peers["vps"]["handshake_age_seconds"] <= 40
    assert peers["phone"]["connected"] is False
    # Public keys are not part of the normalized output.
    assert "PEERPUBKEY" not in str(result)


async def test_services_list_normalized(monkeypatch):
    _configure_opnsense(monkeypatch)

    def handler(request):
        assert request.url.path == "/api/core/service/search"
        return httpx.Response(200, json={"total": 3, "rowCount": 3, "current": 1, "rows": [
            {"id": "unbound", "name": "unbound", "description": "Unbound DNS", "running": 1, "locked": 0},
            {"id": "openssh", "name": "openssh", "description": "Secure Shell", "running": 0, "locked": 0},
            {"id": "wireguard", "name": "wireguard", "description": "WireGuard", "running": 1, "locked": 1},
        ]})

    _base_transport(monkeypatch, handler)
    from app.providers.opnsense.tools import services_list

    result = await services_list()
    assert result["total"] == 3
    assert result["running"] == 2
    assert result["stopped"] == ["openssh"]
    assert result["services"][0]["running"] is True


class _FakeProvider:
    def __init__(self, provider_id: str, ready: bool, status: str = "healthy") -> None:
        self.id = provider_id
        self._ready = ready
        self._status: ProviderStatusValue = cast(ProviderStatusValue, status)

    def ready(self) -> bool:
        return self._ready

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.id, status=self._status)


async def test_lab_overview_aggregates_and_tolerates_failures(monkeypatch):
    fakes = [
        _FakeProvider("proxmox", True, "healthy"),
        _FakeProvider("opnsense", True, "healthy"),
        _FakeProvider("homeassistant", True, "unreachable"),
        _FakeProvider("frigate", False, "unavailable"),
        _FakeProvider("adguard", True, "healthy"),
    ]
    monkeypatch.setattr("app.tools.overview.list_providers", lambda: fakes)

    async def fake_guests():
        return {"guests": [
            {"vmid": 100, "name": "vm-a", "status": "running"},
            {"vmid": 200, "name": "ct-b", "status": "stopped"},
        ]}

    async def fake_gateways():
        return {"total": 2, "offline": ["LTE_GW"]}

    async def fake_wireguard():
        return {"peers_total": 2, "peers_connected": 2, "peers_stale": []}

    async def fake_ha_states():
        raise ProviderError("unreachable", "homeassistant host is unreachable")

    async def fake_adguard_stats():
        return {"stats": {"dns_queries": 5000, "blocked_filtering": 400}}

    monkeypatch.setattr("app.providers.proxmox.tools.guests_list", fake_guests)
    monkeypatch.setattr("app.providers.opnsense.tools.gateway_status", fake_gateways)
    monkeypatch.setattr("app.providers.opnsense.tools.wireguard_status", fake_wireguard)
    monkeypatch.setattr("app.providers.homeassistant.tools.states_summary", fake_ha_states)
    monkeypatch.setattr("app.providers.adguard.tools.stats", fake_adguard_stats)

    from app.tools.overview import lab_overview

    overview = await lab_overview()
    assert overview["providers"]["total"] == 5
    assert overview["providers"]["healthy"] == 3
    assert overview["providers"]["by_status"]["unreachable"] == ["homeassistant"]

    assert overview["proxmox"] == {
        "guests_total": 2, "guests_running": 1, "guests_stopped": ["ct-b"],
    }
    assert overview["network"]["gateways_offline"] == ["LTE_GW"]
    assert overview["network"]["wireguard_peers_connected"] == 2
    # HA failed, but its failure is contained in its own section.
    assert overview["homeassistant"] == {"unavailable": "unreachable"}
    # Frigate not configured at all.
    assert overview["frigate"] == {"unavailable": "not_configured"}
    assert overview["adguard"] == {"dns_queries": 5000, "blocked_filtering": 400}


async def test_lab_overview_runs_through_execution_core(monkeypatch):
    monkeypatch.setattr("app.tools.overview.list_providers", lambda: [])
    from app.domain.actors import Actor
    from app.tools.execution import execute_tool

    result = await execute_tool("lab.overview", {}, Actor(kind="user", id="operator"))
    assert result.ok is True
    assert result.result is not None
    assert result.result["providers"]["total"] == 0
