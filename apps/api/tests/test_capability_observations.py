from datetime import UTC, datetime

import pytest

from app.services.capability_observations import (
    evaluate_availability_observations,
    evaluate_observation,
    list_observation_definitions,
)
from app.tools.execution import ExecutionError, ExecutionResult
from tests.conftest import do_login


def _execution(tool_id: str, result: dict | None = None, *, error: str = "") -> ExecutionResult:
    now = datetime.now(UTC)
    return ExecutionResult(
        ok=not error,
        invocation_id=f"inv-{tool_id}",
        tool_id=tool_id,
        started_at=now,
        finished_at=now,
        duration_ms=1,
        result=result if not error else None,
        error=ExecutionError(code=error, message="normalized failure") if error else None,
    )


def _definition(provider_id: str, capability_id: str):
    return next(
        item
        for item in list_observation_definitions(provider_id)
        if item.capability_id == capability_id
    )


@pytest.mark.parametrize(
    ("provider_id", "capability_id", "result", "expected_status"),
    [
        (
            "cloudflaretunnel",
            "tunnel",
            {"total": 1, "by_status": {"healthy": 0, "degraded": 1, "unavailable": 0}},
            "degraded",
        ),
        (
            "opnsense",
            "gateways",
            {
                "gateways": [{"name": "WAN", "online": True}, {"name": "LTE", "online": False}],
                "offline": ["LTE"],
            },
            "degraded",
        ),
        (
            "opnsense",
            "wireguard",
            {"peers_total": 2, "peers_connected": 0, "peers_stale": ["vps", "phone"]},
            "unavailable",
        ),
        (
            "proxmox",
            "cluster",
            {
                "entries": [
                    {"kind": "cluster", "quorate": False},
                    {"kind": "node", "online": True},
                    {"kind": "node", "online": True},
                ]
            },
            "unavailable",
        ),
        (
            "uptimekuma",
            "monitors",
            {"total": 4, "by_status": {"up": 3, "down": 1}},
            "degraded",
        ),
        (
            "zerotier",
            "members",
            {
                "total": 3,
                "authorized": 2,
                "online": 1,
                "stale": 1,
                "unauthorized": 1,
                "required_total": 2,
                "required_online": 1,
                "required_unavailable": 1,
            },
            "degraded",
        ),
    ],
)
def test_capability_observation_evaluates_normalized_tool_results(
    provider_id, capability_id, result, expected_status
):
    definition = _definition(provider_id, capability_id)
    observation = evaluate_observation(definition, _execution(definition.tool_id, result))

    assert observation.id == f"{provider_id}.{capability_id}"
    assert observation.status == expected_status
    assert observation.error_code == ""


def test_capability_observation_normalizes_execution_failure():
    definition = _definition("opnsense", "wireguard")
    observation = evaluate_observation(
        definition,
        _execution(definition.tool_id, error="provider_timeout"),
    )

    assert observation.status == "unreachable"
    assert observation.error_code == "provider_timeout"
    assert observation.detail == "normalized failure"


def test_zerotier_observation_ignores_offline_intermittent_members():
    definition = _definition("zerotier", "members")
    observation = evaluate_observation(
        definition,
        _execution(
            definition.tool_id,
            {
                "total": 5,
                "authorized": 5,
                "online": 2,
                "stale": 3,
                "unauthorized": 0,
                "required_total": 1,
                "required_online": 1,
                "required_unavailable": 0,
            },
        ),
    )

    assert observation.status == "healthy"
    assert observation.summary["members_stale"] == 3


def test_zerotier_observation_allows_an_empty_on_demand_network():
    definition = _definition("zerotier", "members")
    observation = evaluate_observation(
        definition,
        _execution(
            definition.tool_id,
            {
                "total": 5,
                "authorized": 5,
                "online": 0,
                "stale": 5,
                "unauthorized": 0,
                "required_total": 0,
                "required_online": 0,
                "required_unavailable": 0,
            },
        ),
    )

    assert observation.status == "healthy"
    assert "no always-on members are required" in observation.detail


def test_configured_opnsense_gateways_become_independent_observations(monkeypatch):
    monkeypatch.setattr(
        "app.services.capability_observations.provider_config",
        lambda provider_id: {
            "gateway_observations": [
                {
                    "id": "primary ISP",
                    "label": "primary ISP FWA",
                    "gateway_name": "FASTWEB_DHCP",
                },
                {
                    "id": "backup",
                    "label": "4G backup",
                    "gateway_name": "MIKROTIK_BACKUP_GW",
                },
            ]
        }
        if provider_id == "opnsense"
        else {},
    )
    definitions = {
        item.id: item for item in list_observation_definitions("opnsense")
    }
    execution = _execution(
        "opnsense.gateways.status",
        {
            "gateways": [
                {"name": "FASTWEB_DHCP", "status": "Online", "online": True},
                {
                    "name": "MIKROTIK_BACKUP_GW",
                    "status": "down",
                    "online": False,
                },
            ],
            "offline": ["MIKROTIK_BACKUP_GW"],
        },
    )

    primary = evaluate_observation(
        definitions["opnsense.gateway.primary ISP"], execution
    )
    backup = evaluate_observation(
        definitions["opnsense.gateway.backup"], execution
    )

    assert primary.status == "healthy"
    assert primary.detail == "Gateway FASTWEB_DHCP is online"
    assert primary.summary["reported_status"] == "Online"
    assert backup.status == "unavailable"
    assert backup.detail == "Gateway MIKROTIK_BACKUP_GW is down"


def test_protocol_providers_without_capability_observations_stay_outside_registry():
    provider_ids = {item.provider_id for item in list_observation_definitions()}
    assert provider_ids.isdisjoint(
        {"fritzbox_primary", "fritzbox_secondary", "asterisk", "nutups"}
    )


def test_declared_uptime_monitor_becomes_node_availability_observation(monkeypatch):
    binding = type(
        "Binding",
        (),
        {
            "id": "service.homeassistant",
            "label": "Home Assistant",
            "availability_monitor": "Home Assistant",
        },
    )()
    monkeypatch.setattr(
        "app.services.capability_observations.list_topology_nodes",
        lambda: [binding],
    )
    execution = _execution(
        "uptimekuma.monitors.status",
        {
            "monitors": [
                {
                    "name": "home assistant",
                    "status": "down",
                    "type": "http",
                    "target": "https://ha.example.test",
                }
            ]
        },
    )

    observations = evaluate_availability_observations(execution)

    assert len(observations) == 1
    assert observations[0].id == "uptimekuma.monitor.service.homeassistant"
    assert observations[0].status == "unavailable"
    assert observations[0].summary["monitor_status"] == "down"


async def test_observations_endpoint_routes_every_probe_through_execution_core(
    client, user, capture_adapter, monkeypatch
):
    calls = []

    monkeypatch.setattr(
        "app.services.capability_observations.provider_config",
        lambda provider_id: {
            "gateway_observations": [
                {
                    "id": "primary ISP",
                    "label": "primary ISP FWA",
                    "gateway_name": "WAN",
                },
                {
                    "id": "backup",
                    "label": "Backup WAN",
                    "gateway_name": "LTE",
                },
            ]
        }
        if provider_id == "opnsense"
        else {},
    )

    async def fake_execute(tool_id, raw_input, actor, *, source):
        calls.append((tool_id, raw_input, actor.kind, source))
        results = {
            "cloudflare.tunnels.status": {
                "total": 1,
                "by_status": {"healthy": 1, "degraded": 0, "unavailable": 0},
            },
            "opnsense.gateways.status": {
                "gateways": [
                    {"name": "WAN", "status": "Online", "online": True},
                    {"name": "LTE", "status": "Online", "online": True},
                ],
                "offline": [],
            },
            "opnsense.wireguard.status": {
                "peers_total": 1,
                "peers_connected": 1,
                "peers_stale": [],
            },
            "proxmox.cluster.status": {
                "entries": [
                    {"kind": "cluster", "quorate": True},
                    {"kind": "node", "online": True},
                ]
            },
            "uptimekuma.monitors.status": {
                "total": 1,
                "by_status": {"up": 1},
            },
            "zerotier.members.list": {
                "total": 2,
                "authorized": 2,
                "online": 2,
                "stale": 0,
                "unauthorized": 0,
            },
        }
        return _execution(tool_id, results[tool_id])

    monkeypatch.setattr("app.services.capability_observations.execute_tool", fake_execute)
    monkeypatch.setattr(
        "app.services.capability_observations.list_topology_nodes",
        lambda: [],
    )
    await do_login(client, capture_adapter)

    response = await client.get("/api/observations")

    assert response.status_code == 200
    assert {item["id"] for item in response.json()} == {
        "cloudflaretunnel.tunnel",
        "opnsense.gateways",
        "opnsense.gateway.primary ISP",
        "opnsense.gateway.backup",
        "opnsense.wireguard",
        "proxmox.cluster",
        "uptimekuma.monitors",
        "zerotier.members",
    }
    assert all(raw_input == {} and actor_kind == "user" and source == "rest" for _, raw_input, actor_kind, source in calls)
    assert {tool_id for tool_id, *_ in calls} == {
        "cloudflare.tunnels.status",
        "opnsense.gateways.status",
        "opnsense.wireguard.status",
        "proxmox.cluster.status",
        "uptimekuma.monitors.status",
        "zerotier.members.list",
    }
    assert [tool_id for tool_id, *_ in calls].count("opnsense.gateways.status") == 1


async def test_provider_without_observations_is_empty_without_execution(
    client, user, capture_adapter, monkeypatch
):
    async def unexpected_execute(*args, **kwargs):
        raise AssertionError("providers without observations must not execute a capability tool")

    monkeypatch.setattr("app.api.routes_control.execute_tool", unexpected_execute)
    await do_login(client, capture_adapter)

    response = await client.get("/api/providers/asterisk/observations")

    assert response.status_code == 200
    assert response.json() == []
