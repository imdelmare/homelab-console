"""The old arbitrary-URL / arbitrary-host checks must be gone for good."""

import asyncio

from app.domain.actors import Actor
from app.services.inventory import TlsTargetEntry
from app.tools.execution import execute_tool
from app.tools.registry import get_tool

OPERATOR = Actor(kind="user", id="operator", label="operator")


async def test_arbitrary_url_tool_is_gone():
    assert get_tool("http.health") is None
    result = await execute_tool("http.health", {"url": "http://169.254.169.254/"}, OPERATOR)
    assert result.error is not None
    assert result.error.code == "unknown_tool"


async def test_host_check_rejects_url_field():
    result = await execute_tool(
        "network.host.check", {"host_id": "proxmox", "url": "http://evil.example"}, OPERATOR
    )
    assert result.error is not None
    assert result.error.code == "invalid_input"


async def test_network_clients_list_uses_inventory_only(monkeypatch):
    from app.services.inventory import HostEntry

    monkeypatch.setattr(
        "app.tools.netcheck.list_hosts",
        lambda: [
            HostEntry(
                id="opnsense",
                name="OPNsense",
                address="10.0.0.1",
                kind="router",
                tags=["network", "firewall"],
                check_ports=[443],
            )
        ],
    )
    result = await execute_tool("network.clients.list", {"tag": "network"}, OPERATOR)
    assert result.ok is True
    assert result.result is not None
    assert result.result["source"] == "inventory"
    assert result.result["total"] >= 1
    assert all("id" in client for client in result.result["clients"])
    assert all("address" in client for client in result.result["clients"])


async def test_tls_certificate_probes_run_concurrently(monkeypatch):
    from app.tools import netcheck

    active = 0
    peak = 0

    async def fake_probe(target):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return {
            "id": target.id,
            "ok": True,
            "days_until_expiry": 30,
            "warning_days": target.warning_days,
        }

    monkeypatch.setattr(
        netcheck,
        "list_tls_targets",
        lambda: [TlsTargetEntry(id=f"target-{index}", host="example.test") for index in range(4)],
    )
    monkeypatch.setattr(netcheck, "_probe_certificate", fake_probe)

    result = await netcheck.tls_certificates()

    assert result["total"] == 4
    assert peak == 4


async def test_network_clients_list_rejects_arbitrary_address_filter():
    result = await execute_tool(
        "network.clients.list", {"address": "169.254.169.254"}, OPERATOR
    )
    assert result.error is not None
    assert result.error.code == "invalid_input"


async def test_host_check_rejects_arbitrary_address():
    # Addresses are not accepted at all — only inventory IDs.
    result = await execute_tool("network.host.check", {"host_id": "169.254.169.254"}, OPERATOR)
    assert result.ok is False or (result.result is not None and result.result.get("error") == "unknown_host_id")


async def test_dns_resolve_rejects_arbitrary_domain_field():
    result = await execute_tool(
        "network.dns.resolve",
        {"target_id": "google", "domain": "evil.example"},
        OPERATOR,
    )
    assert result.error is not None
    assert result.error.code == "invalid_input"


async def test_dns_resolve_unknown_target_does_not_query_network():
    result = await execute_tool("network.dns.resolve", {"target_id": "not-in-config"}, OPERATOR)
    assert result.ok is True
    assert result.result is not None
    assert result.result["ok"] is False
    assert result.result["error"] == "unknown_dns_target"


async def test_dns_resolve_rejects_arbitrary_resolver_address():
    result = await execute_tool(
        "network.dns.resolve",
        {"target_id": "google", "resolver": "169.254.169.254"},
        OPERATOR,
    )
    assert result.error is not None
    assert result.error.code == "invalid_input"


async def test_host_check_unknown_inventory_id():
    result = await execute_tool("network.host.check", {"host_id": "not-in-inventory"}, OPERATOR)
    # The execution pipeline ran, but the check itself reports the unknown ID
    # without touching the network.
    assert result.result is not None
    assert result.result["ok"] is False
    assert result.result["error"] == "unknown_host_id"


async def test_host_check_rejects_excessive_timeout():
    result = await execute_tool(
        "network.host.check", {"host_id": "proxmox", "timeout": 60}, OPERATOR
    )
    assert result.error is not None
    assert result.error.code == "invalid_input"


async def test_host_check_rejects_excessive_port_list():
    result = await execute_tool(
        "network.host.check",
        {"host_id": "proxmox", "ports": list(range(1, 100))},
        OPERATOR,
    )
    assert result.error is not None
    assert result.error.code == "invalid_input"


async def test_no_generic_tools_registered():
    from app.tools.registry import list_tools

    forbidden_fragments = ("shell", "ssh.exec", "http.request", "docker", "api.forward")
    for tool in list_tools():
        for fragment in forbidden_fragments:
            assert fragment not in tool.id, f"generic capability leaked: {tool.id}"
