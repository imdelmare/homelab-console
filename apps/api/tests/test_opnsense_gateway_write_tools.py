"""Focused tests for controlled OPNsense gateway transitions."""

import pytest
from pydantic import ValidationError

from app.providers.opnsense import tools as opnsense_tools
from app.tools.governance import APPROVED_WRITE_TOOLS
from app.tools.registry import EmptyInput, get_tool


BOUNDARY = {
    "gateway_failover": {
        "primary": {
            "name": "FASTWEB_DHCP",
            "uuid": "11111111-1111-1111-1111-111111111111",
        },
        "backup": {
            "name": "MIKROTIK_BACKUP_GW",
            "uuid": "22222222-2222-2222-2222-222222222222",
        },
    }
}


def test_gateway_tools_are_active_under_adr_0010():
    for tool_id in ("opnsense.gateway.failover", "opnsense.gateway.restore"):
        assert (
            APPROVED_WRITE_TOOLS[tool_id]
            == "docs/decisions/0010-activate-opnsense-gateway-drill.md"
        )
        tool = get_tool(tool_id)
        assert tool is not None and tool.enabled is True


def test_gateway_tools_are_forced_disabled_without_governance(monkeypatch):
    for tool_id in ("opnsense.gateway.failover", "opnsense.gateway.restore"):
        monkeypatch.delitem(APPROVED_WRITE_TOOLS, tool_id)
        tool = get_tool(tool_id)
        assert tool is not None and tool.enabled is False


def test_egress_switch_remains_disabled_after_adr_0011_drill():
    assert "opnsense.egress.switch" not in APPROVED_WRITE_TOOLS
    tool = get_tool("opnsense.egress.switch")
    assert tool is not None and tool.enabled is False


def test_gateway_tools_accept_no_caller_selected_target():
    assert EmptyInput.model_validate({}).model_dump() == {}
    with pytest.raises(ValidationError):
        EmptyInput.model_validate({"gateway": "attacker-controlled"})


async def test_failover_uses_only_declared_uuids_and_verifies_route(monkeypatch):
    calls = []
    snapshots = [
        {
            "gateways": [
                {
                    "uuid": BOUNDARY["gateway_failover"]["primary"]["uuid"],
                    "name": "FASTWEB_DHCP",
                    "enabled": True,
                },
                {
                    "uuid": BOUNDARY["gateway_failover"]["backup"]["uuid"],
                    "name": "MIKROTIK_BACKUP_GW",
                    "enabled": True,
                },
            ],
            "default_routes": [{"gateway": "100.64.0.1"}],
        },
        {
            "gateways": [
                {
                    "uuid": BOUNDARY["gateway_failover"]["primary"]["uuid"],
                    "name": "FASTWEB_DHCP",
                    "enabled": False,
                },
                {
                    "uuid": BOUNDARY["gateway_failover"]["backup"]["uuid"],
                    "name": "MIKROTIK_BACKUP_GW",
                    "enabled": True,
                },
            ],
            "default_routes": [{"gateway": "10.10.0.1"}],
        },
    ]

    class Client:
        def __init__(self, profile):
            assert profile == "wol"

        async def post(self, path):
            calls.append(path)
            return {"status": "ok"}

    monkeypatch.setattr(opnsense_tools, "provider_config", lambda _id: BOUNDARY)
    monkeypatch.setattr(opnsense_tools, "OpnsenseClient", Client)
    monkeypatch.setattr(
        opnsense_tools,
        "gateway_configuration",
        lambda: _next_snapshot(snapshots),
    )

    async def status():
        return {
            "gateways": [
                {
                    "name": "MIKROTIK_BACKUP_GW",
                    "address": "10.10.0.1",
                    "online": True,
                }
            ]
        }

    monkeypatch.setattr(opnsense_tools, "gateway_status", status)
    result = await opnsense_tools.gateway_transition("failover")

    assert calls == [
        "/api/routing/settings/toggle_gateway/"
        "11111111-1111-1111-1111-111111111111/0",
        "/api/routing/settings/reconfigure",
    ]
    assert result["target_gateway"] == "MIKROTIK_BACKUP_GW"
    assert result["verified"] is True


async def test_restore_enables_primary_and_verifies_route(monkeypatch):
    calls = []
    snapshots = [
        {
            "gateways": [
                {
                    "uuid": BOUNDARY["gateway_failover"]["primary"]["uuid"],
                    "name": "FASTWEB_DHCP",
                    "enabled": False,
                },
                {
                    "uuid": BOUNDARY["gateway_failover"]["backup"]["uuid"],
                    "name": "MIKROTIK_BACKUP_GW",
                    "enabled": True,
                },
            ],
            "default_routes": [{"gateway": "10.10.0.1"}],
        },
        {
            "gateways": [
                {
                    "uuid": BOUNDARY["gateway_failover"]["primary"]["uuid"],
                    "name": "FASTWEB_DHCP",
                    "enabled": True,
                },
                {
                    "uuid": BOUNDARY["gateway_failover"]["backup"]["uuid"],
                    "name": "MIKROTIK_BACKUP_GW",
                    "enabled": True,
                },
            ],
            "default_routes": [{"gateway": "100.64.0.1"}],
        },
    ]

    class Client:
        def __init__(self, profile):
            assert profile == "wol"

        async def post(self, path):
            calls.append(path)
            return {"status": "ok"}

    monkeypatch.setattr(opnsense_tools, "provider_config", lambda _id: BOUNDARY)
    monkeypatch.setattr(opnsense_tools, "OpnsenseClient", Client)
    monkeypatch.setattr(
        opnsense_tools,
        "gateway_configuration",
        lambda: _next_snapshot(snapshots),
    )

    async def status():
        return {
            "gateways": [
                {
                    "name": "FASTWEB_DHCP",
                    "address": "100.64.0.1",
                    "online": True,
                }
            ]
        }

    monkeypatch.setattr(opnsense_tools, "gateway_status", status)
    result = await opnsense_tools.gateway_transition("restore")

    assert calls == [
        "/api/routing/settings/toggle_gateway/"
        "11111111-1111-1111-1111-111111111111/1",
        "/api/routing/settings/reconfigure",
    ]
    assert result["target_gateway"] == "FASTWEB_DHCP"
    assert result["primary_enabled"] is True
    assert result["verified"] is True


async def _next_snapshot(snapshots):
    return snapshots.pop(0)
