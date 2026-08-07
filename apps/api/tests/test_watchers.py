from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import (
    AuditEvent,
    Incident,
    Task,
    TaskCheck,
    TaskEvent,
    WatcherAutomationState,
    WatcherConfig,
    WatcherRun,
)
from app.db.session import get_session_factory
from app.domain.actors import Actor
from app.services import dependency_graph, inventory
from app.services.audit import write_audit
from app.services.tasks_service import (
    claim_task,
    list_resolutions,
    set_status,
    task_detail,
    task_resolution_labels,
)
from app.services.watchers import (
    DetectedIncident,
    _cloudflare_tunnel_incidents,
    _incident_from_finding,
    _network_gateway_incidents,
    _network_presence_incidents,
    _network_presence_snapshot,
    _network_zerotier_incidents,
    _task_goal,
    _upsert_incident,
    configure_watcher,
    reset_runtime_state_for_tests,
    resolve_incident_as_handled,
    run_watchers,
)


def test_watcher_task_goal_is_an_english_operator_message():
    goal = _task_goal(
        DetectedIncident(
            watcher_id="gateway",
            dedupe_key="gateway-down",
            dedupe_basis="gateway-down",
            severity="critical",
            provider_id="opnsense",
            title="Gateway down",
            description="The primary gateway is unreachable.",
            payload={},
        )
    )

    assert "Watcher `gateway` detected a critical alert on `opnsense`." in goal
    assert "Detail: The primary gateway is unreachable." in goal
    assert "Check the status with read-only summary tools" in goal
from app.tools.execution import ExecutionError, ExecutionResult
from tests.conftest import do_login

WATCHER_OPERATOR = Actor(kind="user", id="operator", label="operator")


@pytest.fixture(autouse=True)
def disable_task_router_for_watcher_tests(monkeypatch):
    async def fake_enqueue_task_routing(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.watchers.enqueue_task_routing", fake_enqueue_task_routing)


def _tool_result(findings):
    now = datetime.now(UTC)
    return ExecutionResult(
        ok=True,
        invocation_id="inv-watch",
        tool_id="lab.alerts.recent",
        started_at=now,
        finished_at=now,
        duration_ms=1,
        result={
            "summary": {
                "provider_id": "lab.alerts",
                "status": "degraded",
                "severity": "critical",
                "metrics": {"alerts_total": len(findings)},
                "findings": findings,
            }
        },
    )


def _plain_tool_result(tool_id, result):
    now = datetime.now(UTC)
    return ExecutionResult(
        ok=True,
        invocation_id=f"inv-{tool_id}",
        tool_id=tool_id,
        started_at=now,
        finished_at=now,
        duration_ms=1,
        result=result,
    )


def _failed_tool_result(tool_id):
    now = datetime.now(UTC)
    return ExecutionResult(
        ok=False,
        invocation_id=f"inv-{tool_id}",
        tool_id=tool_id,
        started_at=now,
        finished_at=now,
        duration_ms=1,
        error=ExecutionError(code="provider_timeout", message=f"{tool_id} timed out"),
    )


def _gateway_policy(
    gateway_name: str,
    observation_id: str,
    *,
    availability_group: str = "wan",
    performance_monitoring: bool = True,
) -> dict:
    return {
        "gateway_name": gateway_name,
        "observation_id": observation_id,
        "availability_group": availability_group,
        "group_mode": "any",
        "performance_monitoring": performance_monitoring,
        "loss_warning_percent": 10.0,
        "loss_critical_percent": 50.0,
        "rtt_warning_ms": 150.0,
        "jitter_warning_ms": 30.0,
    }


def _configure_graph(monkeypatch, nodes: list[dict]) -> None:
    entries = [inventory.DependencyEntry(**item) for item in nodes]
    monkeypatch.setattr(inventory, "list_dependencies", lambda: entries)
    dependency_graph.clear_cache()


async def test_correlated_findings_produce_one_task(db_session, monkeypatch):
    _configure_graph(
        monkeypatch,
        [
            {"id": "opnsense", "depends_on": []},
            {"id": "homeassistant", "depends_on": ["opnsense"]},
        ],
    )
    findings = [
        {"provider_id": "opnsense", "severity": "critical", "message": "Gateway unreachable"},
        {"provider_id": "homeassistant", "severity": "warning", "message": "Home Assistant unreachable"},
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(findings)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session)

    assert result["created_tasks"] == 1
    assert result["updated_incidents"] == 1

    task_count = (await db_session.execute(select(func.count()).select_from(Task))).scalar_one()
    assert task_count == 1

    incidents = (await db_session.execute(select(Incident))).scalars().all()
    assert len(incidents) == 2
    by_provider = {incident.provider_id: incident for incident in incidents}
    assert len({incident.task_id for incident in incidents}) == 1
    assert by_provider["opnsense"].root_cause_incident_id is None
    assert by_provider["homeassistant"].root_cause_incident_id == by_provider["opnsense"].id


async def test_uncorrelated_findings_still_produce_independent_tasks(db_session, monkeypatch):
    _configure_graph(
        monkeypatch,
        [
            {"id": "opnsense", "depends_on": []},
            {"id": "homeassistant", "depends_on": ["opnsense"]},
            {"id": "vps", "depends_on": []},
        ],
    )
    findings = [
        {"provider_id": "vps", "severity": "critical", "message": "VPS deploy failed"},
        {"provider_id": "homeassistant", "severity": "warning", "message": "Home Assistant unreachable"},
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(findings)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session)

    assert result["created_tasks"] == 2
    assert result["updated_incidents"] == 0

    task_count = (await db_session.execute(select(func.count()).select_from(Task))).scalar_one()
    assert task_count == 2

    incidents = (await db_session.execute(select(Incident))).scalars().all()
    assert len(incidents) == 2
    assert all(incident.root_cause_incident_id is None for incident in incidents)
    assert len({incident.task_id for incident in incidents}) == 2


async def test_warning_upstream_does_not_swallow_downstream_findings(db_session, monkeypatch):
    _configure_graph(
        monkeypatch,
        [
            {"id": "fritzbox_primary", "depends_on": []},
            {"id": "opnsense", "depends_on": ["fritzbox_primary"]},
            {"id": "homeassistant", "depends_on": ["opnsense"]},
        ],
    )
    findings = [
        {
            "provider_id": "fritzbox_primary",
            "severity": "warning",
            "message": "1 disabled Wi-Fi radio",
        },
        {
            "provider_id": "homeassistant",
            "severity": "warning",
            "message": "Home Assistant errors",
        },
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(findings)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session)

    assert result["created_tasks"] == 2
    assert result["updated_incidents"] == 0

    incidents = (await db_session.execute(select(Incident))).scalars().all()
    assert len(incidents) == 2
    assert all(incident.root_cause_incident_id is None for incident in incidents)
    assert len({incident.task_id for incident in incidents}) == 2


async def test_watcher_creates_task_and_dedupes_open_incident(db_session, monkeypatch):
    findings = [
        {
            "provider_id": "uptimekuma",
            "severity": "critical",
            "message": "Nextcloud monitor is down",
        }
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        assert actor.kind == "service"
        assert source == "watcher"
        if tool_id == "opnsense.gateways.status":
            return _plain_tool_result(tool_id, {"gateways": [], "total": 0, "offline": []})
        if tool_id == "uptimekuma.monitors.status":
            return _plain_tool_result(tool_id, {"monitors": [], "total": 0, "by_status": {}})
        if tool_id == "opnsense.wireguard.status":
            return _plain_tool_result(tool_id, {"peers_total": 0, "peers_connected": 0, "peers_stale": []})
        if tool_id == "nutups.status":
            return _plain_tool_result(
                tool_id,
                {
                    "ups": {
                        "name": "ups",
                        "status": "online",
                        "status_flags": ["OL"],
                        "battery_charge_percent": 100,
                        "battery_runtime_seconds": 3600,
                        "load_percent": 2,
                    }
                },
            )
        if tool_id == "cloudflare.summary":
            return _plain_tool_result(
                tool_id,
                {"summary": {"metrics": {"tunnels_total": 1, "connections_active": 1}}},
            )
        if tool_id == "zerotier.members.list":
            return _plain_tool_result(
                tool_id,
                {"required_total": 1, "required_online": 1, "required_unavailable": 0},
            )
        assert tool_id == "lab.alerts.recent"
        return _tool_result(findings)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    first = await run_watchers(db_session)
    second = await run_watchers(db_session)

    assert first["ok"] is True
    assert first["created_tasks"] == 1
    assert second["ok"] is True
    assert second["created_tasks"] == 0
    assert second["updated_incidents"] == 1

    task_count = (await db_session.execute(select(func.count()).select_from(Task))).scalar_one()
    assert task_count == 1
    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.status == "open"
    assert incident.occurrences == 2
    assert incident.task_id
    assert incident.provider_id == "uptimekuma"


async def test_watcher_resolves_incident_when_alert_clears(db_session, monkeypatch):
    findings = [
        {
            "provider_id": "uptimekuma",
            "severity": "critical",
            "message": "Nextcloud monitor is down",
        }
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(findings)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    first = await run_watchers(db_session)
    findings.clear()
    second = await run_watchers(db_session)
    third = await run_watchers(db_session)
    fourth = await run_watchers(db_session)

    assert first["created_tasks"] == 1
    assert second["created_tasks"] == 0
    assert second["updated_incidents"] == 0
    assert second["resolved_incidents"] == 0
    assert third["resolved_incidents"] == 0
    assert fourth["resolved_incidents"] == 1
    runs = (
        await db_session.execute(
            select(WatcherRun)
            .where(WatcherRun.watcher_id == "lab.alerts")
            .order_by(WatcherRun.started_at)
        )
    ).scalars().all()
    assert runs[1].payload["clearing_total"] == 1
    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.status == "resolved"
    assert incident.resolved_at is not None
    assert incident.resolution_reason == "alert_cleared"
    assert incident.missing_runs == 3
    task = await db_session.get(Task, incident.task_id)
    assert task.status == "completed"
    assert "Auto-resolved by watcher" in task.summary
    labels = await task_resolution_labels(db_session, [task.id])
    assert labels[task.id] == "auto_closed"
    detail = await task_detail(db_session, task)
    assert detail["resolution_label"] == "auto_closed"
    assert detail["auto_closed"] is True
    assert detail["assigned_agent"] == ""
    resolutions = await list_resolutions(db_session, task.id)
    assert len(resolutions) == 1
    assert resolutions[0].source == "watcher"


async def test_watcher_does_not_auto_complete_claimed_task_when_alert_clears(
    db_session, monkeypatch
):
    findings = [
        {
            "provider_id": "uptimekuma",
            "severity": "critical",
            "message": "Nextcloud monitor is down",
        }
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(findings)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    await run_watchers(db_session)
    incident = (await db_session.execute(select(Incident))).scalar_one()
    await claim_task(
        db_session,
        incident.task_id,
        "agent:codex",
        Actor(kind="agent", id="codex", label="Codex"),
    )
    findings.clear()
    await run_watchers(db_session)
    await run_watchers(db_session)
    await run_watchers(db_session)

    incident = await db_session.get(Incident, incident.id)
    assert incident.status == "resolved"
    task = await db_session.get(Task, incident.task_id)
    assert task.status == "claimed"
    assert task.assigned_agent == "agent:codex"
    assert await list_resolutions(db_session, task.id) == []


async def test_watcher_flapping_before_grace_period_reuses_task(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.watchers.get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "watchers_enabled": True,
                "watchers_interval_seconds": 300,
                "watchers_min_severity": "warning",
                "watchers_ignore_pattern_list": [],
                "watchers_resolve_after_missing_runs": 3,
            },
        )(),
    )

    findings = [
        {
            "provider_id": "uptimekuma",
            "severity": "warning",
            "message": "Nextcloud monitor is down",
        }
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(findings)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    first = await run_watchers(db_session)
    findings.clear()
    missing = await run_watchers(db_session)
    findings.append(
        {
            "provider_id": "uptimekuma",
            "severity": "critical",
            "message": "Nextcloud monitor is down",
        }
    )
    returned = await run_watchers(db_session)

    assert first["created_tasks"] == 1
    assert missing["resolved_incidents"] == 0
    assert returned["created_tasks"] == 0
    assert returned["updated_incidents"] == 1
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 1
    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.status == "open"
    assert incident.severity == "critical"
    assert incident.occurrences == 2
    assert incident.missing_runs == 0
    assert incident.last_missing_at is None


async def test_watcher_dedupes_variable_count_findings(db_session, monkeypatch):
    findings = [
        {
            "provider_id": "homeassistant",
            "severity": "warning",
            "message": "193 Home Assistant entity/entities unavailable or unknown",
        }
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(findings)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    first = await run_watchers(db_session)
    findings[0]["message"] = "197 Home Assistant entity/entities unavailable or unknown"
    second = await run_watchers(db_session)

    assert first["created_tasks"] == 1
    assert second["created_tasks"] == 0
    assert second["updated_incidents"] == 1
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 1
    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.occurrences == 2
    assert "197 Home Assistant" in incident.description


async def test_network_presence_first_run_establishes_baseline(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        if tool_id == "opnsense.devices.arp":
            return _plain_tool_result(
                tool_id,
                {"devices": [{"ip_address": "10.0.0.50", "mac_address": "aa:bb:cc:dd:ee:01", "hostname": "phone"}]},
            )
        if tool_id == "opnsense.kea.leases":
            return _plain_tool_result(
                tool_id,
                {"leases": [{"ip_address": "10.0.0.50", "mac_address": "aa:bb:cc:dd:ee:01", "hostname": "phone"}]},
            )
        raise AssertionError(tool_id)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"network.presence"})

    assert result["ok"] is True
    assert result["created_tasks"] == 0
    assert result["watchers"][0]["payload"]["baseline"] is True
    assert result["watchers"][0]["payload"]["observed_macs"] == ["aa:bb:cc:dd:ee:01"]


async def test_network_presence_detects_new_device_after_baseline(db_session, monkeypatch):
    devices = [{"ip_address": "10.0.0.50", "mac_address": "aa:bb:cc:dd:ee:01", "hostname": "phone"}]
    leases = [{"ip_address": "10.0.0.50", "mac_address": "aa:bb:cc:dd:ee:01", "hostname": "phone"}]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        if tool_id == "opnsense.devices.arp":
            return _plain_tool_result(tool_id, {"devices": devices})
        if tool_id == "opnsense.kea.leases":
            return _plain_tool_result(tool_id, {"leases": leases})
        raise AssertionError(tool_id)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    baseline = await run_watchers(db_session, watcher_ids={"network.presence"})
    devices.append({"ip_address": "10.0.0.77", "mac_address": "aa-bb-cc-dd-ee-02", "hostname": "tablet"})
    leases.append({"ip_address": "10.0.0.77", "mac_address": "aa:bb:cc:dd:ee:02", "hostname": "tablet"})
    changed = await run_watchers(db_session, watcher_ids={"network.presence"})

    assert baseline["created_tasks"] == 0
    assert changed["created_tasks"] == 1
    assert changed["watchers"][0]["payload"]["new_macs_total"] == 1
    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.watcher_id == "network.presence"
    assert incident.provider_id == "opnsense"
    assert "New network device observed" in incident.title


def test_network_presence_ignores_expired_leases_and_downgrades_arp_kea_transition():
    old_mac = "20:28:bc:b9:6f:e2"
    new_mac = "64:e4:a5:eb:28:de"
    snapshot = _network_presence_snapshot(
        [{"ip_address": "10.0.0.40", "mac_address": old_mac, "hostname": "tv-lg-webos"}],
        [
            {
                "ip_address": "10.0.0.40",
                "mac_address": new_mac,
                "hostname": "tv-lg",
                "state": "active",
                "valid_lifetime_seconds": 4000,
            },
            {
                "ip_address": "10.0.0.42",
                "mac_address": old_mac,
                "state": "2",
                "valid_lifetime_seconds": 0,
            },
        ],
    )

    incidents = _network_presence_incidents(snapshot, {old_mac, new_mac})
    assert "10.0.0.42" not in snapshot["observed_ips"]
    assert not any(item.payload.get("code") == "duplicate_ip" for item in incidents)
    mismatch = next(item for item in incidents if item.payload.get("code") == "arp_kea_mismatch")
    assert mismatch.severity == "warning"
    assert mismatch.payload["arp_macs"] == [old_mac]
    assert mismatch.payload["kea_macs"] == [new_mac]


def test_network_presence_keeps_real_duplicate_binding_critical():
    snapshot = _network_presence_snapshot(
        [
            {"ip_address": "10.0.0.40", "mac_address": "20:28:bc:b9:6f:e2"},
            {"ip_address": "10.0.0.40", "mac_address": "64:e4:a5:eb:28:de"},
        ],
        [],
    )

    incidents = _network_presence_incidents(
        snapshot, {"20:28:bc:b9:6f:e2", "64:e4:a5:eb:28:de"}
    )
    duplicate = next(item for item in incidents if item.payload.get("code") == "duplicate_ip")
    assert duplicate.severity == "critical"
    assert duplicate.payload["arp_macs"] == ["20:28:bc:b9:6f:e2", "64:e4:a5:eb:28:de"]


async def test_network_gateway_detects_offline_gateway(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        assert tool_id == "opnsense.gateways.status"
        return _plain_tool_result(
            tool_id,
            {
                "gateways": [
                    {
                        "name": "WAN_DHCP",
                        "address": "192.0.2.1",
                        "online": False,
                        "loss_percent": 100,
                        "rtt_ms": None,
                    }
                ],
                "offline": ["WAN_DHCP"],
            },
        )

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"network.gateway"})

    assert result["created_tasks"] == 1
    assert result["updated_incidents"] == 0
    incidents = (
        await db_session.execute(select(Incident).where(Incident.watcher_id == "network.gateway"))
    ).scalars().all()
    incident = next(item for item in incidents if "gateway offline" in item.title.lower())
    assert incident.watcher_id == "network.gateway"
    assert incident.severity == "critical"
    assert "gateway offline" in incident.title.lower()


async def test_network_gateway_detects_degraded_jitter(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        assert tool_id == "opnsense.gateways.status"
        return _plain_tool_result(
            tool_id,
            {
                "gateways": [
                    {
                        "name": "WAN_DHCP",
                        "address": "192.0.2.1",
                        "online": True,
                        "loss_percent": 0,
                        "rtt_ms": 42,
                        "rtt_stddev_ms": 35,
                    }
                ],
                "offline": [],
            },
        )

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"network.gateway"})

    assert result["created_tasks"] == 1
    incident = (
        await db_session.execute(select(Incident).where(Incident.watcher_id == "network.gateway"))
    ).scalar_one()
    assert incident.severity == "warning"
    assert "performance degraded" in incident.title.lower()


def test_network_gateway_dual_wan_degrades_only_failed_link(monkeypatch):
    monkeypatch.setattr(
        "app.services.watchers._gateway_watch_policies",
        lambda _rows: [
            _gateway_policy("FASTWEB_DHCP", "opnsense.gateway.primary"),
            _gateway_policy("MIKROTIK_BACKUP_GW", "opnsense.gateway.backup"),
        ],
    )

    incidents = _network_gateway_incidents(
        {
            "gateways": [
                {"name": "FASTWEB_DHCP", "online": False, "loss_percent": 100},
                {"name": "MIKROTIK_BACKUP_GW", "online": True, "loss_percent": 0},
            ]
        }
    )

    assert len(incidents) == 1
    assert incidents[0].severity == "warning"
    assert incidents[0].payload["code"] == "gateway_offline"
    assert incidents[0].payload["observation_id"] == "opnsense.gateway.primary"


def test_network_gateway_dual_wan_critical_only_when_group_is_down(monkeypatch):
    monkeypatch.setattr(
        "app.services.watchers._gateway_watch_policies",
        lambda _rows: [
            _gateway_policy("FASTWEB_DHCP", "opnsense.gateway.primary"),
            _gateway_policy("MIKROTIK_BACKUP_GW", "opnsense.gateway.backup"),
        ],
    )

    incidents = _network_gateway_incidents(
        {
            "gateways": [
                {"name": "FASTWEB_DHCP", "online": False, "loss_percent": 100},
                {"name": "MIKROTIK_BACKUP_GW", "online": False, "loss_percent": 100},
            ]
        }
    )

    assert len(incidents) == 1
    assert incidents[0].severity == "critical"
    assert incidents[0].payload["code"] == "gateway_group_unavailable"
    assert incidents[0].payload["observation_ids"] == [
        "opnsense.gateway.primary",
        "opnsense.gateway.backup",
    ]


def test_network_gateway_ignores_backup_performance_noise(monkeypatch):
    monkeypatch.setattr(
        "app.services.watchers._gateway_watch_policies",
        lambda _rows: [
            _gateway_policy(
                "MIKROTIK_BACKUP_GW",
                "opnsense.gateway.backup",
                performance_monitoring=False,
            )
        ],
    )

    incidents = _network_gateway_incidents(
        {
            "gateways": [
                {
                    "name": "MIKROTIK_BACKUP_GW",
                    "online": True,
                    "loss_percent": 0,
                    "rtt_ms": 408,
                    "rtt_stddev_ms": 927,
                }
            ]
        }
    )

    assert incidents == []


def test_zerotier_watcher_only_alerts_on_required_members():
    assert _network_zerotier_incidents(
        {
            "total": 5,
            "online": 0,
            "stale": 5,
            "required_total": 0,
            "required_online": 0,
            "required_unavailable": 0,
        }
    ) == []

    assert _network_zerotier_incidents(
        {
            "total": 4,
            "stale": 3,
            "required_total": 1,
            "required_online": 1,
            "required_unavailable": 0,
        }
    ) == []

    incidents = _network_zerotier_incidents(
        {"required_total": 1, "required_online": 0, "required_unavailable": 1}
    )
    assert len(incidents) == 1
    assert incidents[0].severity == "critical"
    assert incidents[0].payload["observation_id"] == "zerotier.members"


def test_cloudflare_watcher_emits_single_authoritative_incident():
    assert _cloudflare_tunnel_incidents(
        {"summary": {"metrics": {"tunnels_total": 1, "connections_active": 4}}}
    ) == []

    incidents = _cloudflare_tunnel_incidents(
        {
            "summary": {
                "metrics": {
                    "tunnels_total": 1,
                    "tunnels_unavailable": 1,
                    "connections_active": 0,
                }
            }
        }
    )
    assert len(incidents) == 1
    assert incidents[0].severity == "critical"
    assert incidents[0].payload["code"] == "tunnel_unavailable"
    assert incidents[0].payload["observation_id"] == "cloudflaretunnel.tunnel"


async def test_network_wireguard_detects_stale_peer(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        assert tool_id == "opnsense.wireguard.status"
        return _plain_tool_result(
            tool_id,
            {"peers_total": 2, "peers_connected": 1, "peers_stale": ["vps"]},
        )

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"network.wireguard"})

    assert result["created_tasks"] == 1
    incident = (
        await db_session.execute(select(Incident).where(Incident.watcher_id == "network.wireguard"))
    ).scalar_one()
    assert incident.watcher_id == "network.wireguard"
    assert incident.severity == "warning"
    assert "WireGuard stale peer" in incident.title


async def test_power_ups_watcher_detects_low_battery(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        assert tool_id == "nutups.status"
        return _plain_tool_result(
            tool_id,
            {
                "ups": {
                    "name": "ups",
                    "status": "low_battery",
                    "status_flags": ["OB", "LB"],
                    "battery_charge_percent": 22,
                    "battery_runtime_seconds": 420,
                    "load_percent": 18,
                    "model": "UPS Proxmox",
                }
            },
        )

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"power.ups"})

    assert result["created_tasks"] == 1
    incidents = (
        await db_session.execute(select(Incident).where(Incident.watcher_id == "power.ups"))
    ).scalars().all()
    assert len(incidents) == 3
    assert {incident.provider_id for incident in incidents} == {"nutups"}
    assert {incident.severity for incident in incidents} == {"critical", "warning"}
    assert {incident.payload["runbook_incident_type"] for incident in incidents} == {"power_alert"}


async def test_power_ups_watcher_resolves_when_online(db_session, monkeypatch):
    responses = [
        {
            "ups": {
                "name": "ups",
                "status": "on_battery",
                "status_flags": ["OB"],
                "battery_charge_percent": 80,
                "battery_runtime_seconds": 1200,
                "load_percent": 15,
            }
        },
        {
            "ups": {
                "name": "ups",
                "status": "online",
                "status_flags": ["OL"],
                "battery_charge_percent": 100,
                "battery_runtime_seconds": 3600,
                "load_percent": 2,
            }
        },
        {
            "ups": {
                "name": "ups",
                "status": "online",
                "status_flags": ["OL"],
                "battery_charge_percent": 100,
                "battery_runtime_seconds": 3600,
                "load_percent": 2,
            }
        },
        {
            "ups": {
                "name": "ups",
                "status": "online",
                "status_flags": ["OL"],
                "battery_charge_percent": 100,
                "battery_runtime_seconds": 3600,
                "load_percent": 2,
            }
        },
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        assert tool_id == "nutups.status"
        return _plain_tool_result(tool_id, responses.pop(0))

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    first = await run_watchers(db_session, watcher_ids={"power.ups"})
    second = await run_watchers(db_session, watcher_ids={"power.ups"})
    third = await run_watchers(db_session, watcher_ids={"power.ups"})
    fourth = await run_watchers(db_session, watcher_ids={"power.ups"})

    assert first["created_tasks"] == 1
    assert second["resolved_incidents"] == 0
    assert third["resolved_incidents"] == 0
    assert fourth["resolved_incidents"] == 1
    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.status == "resolved"
    task = await db_session.get(Task, incident.task_id)
    assert task.status == "completed"


async def test_uptimekuma_monitor_detects_down_monitor(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        assert tool_id == "uptimekuma.monitors.status"
        return _plain_tool_result(
            tool_id,
            {
                "total": 2,
                "by_status": {"up": 1, "down": 1},
                "monitors": [
                    {"name": "Nextcloud", "type": "http", "target": "https://nc.example/status.php", "status": "down"},
                    {"name": "OPNsense", "type": "ping", "target": "10.0.0.1", "status": "up"},
                ],
            },
        )

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"uptimekuma.monitors"})

    assert result["created_tasks"] == 1
    incident = (
        await db_session.execute(select(Incident).where(Incident.watcher_id == "uptimekuma.monitors"))
    ).scalar_one()
    assert incident.provider_id == "uptimekuma"
    assert incident.severity == "critical"
    assert "Nextcloud" in incident.title


async def test_thermal_watcher_detects_thresholds(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        payloads = {
            "nutups.status": {
                "ups": {
                    "name": "ups",
                    "status": "online",
                    "ups_temperature_c": 39,
                    "load_percent": 12,
                    "battery_charge_percent": 100,
                }
            },
            "opnsense.system.temperature": {
                "temperature": {"sensors": [{"sensor_id": "cpu0", "kind": "cpu", "temperature_c": 72}]}
            },
            "mikrotik.system.health": {
                "health": {
                    "temperature_sensors": [{"sensor_id": "board", "kind": "board", "temperature_c": 69}],
                    "voltage_v": 24.1,
                }
            },
            "fritzbox.primary.temperature": {
                "temperature": {"supported": True, "sensors": [{"sensor_id": "cpu", "kind": "cpu", "temperature_c": 77}]}
            },
            "fritzbox.secondary.temperature": {
                "temperature": {"supported": False, "sensors": [], "maximum_temperature_c": None}
            },
            "proxmox.disks.temperatures": {
                "disks": [{"node": "pve", "devpath": "/dev/sda", "disk_model": "PNY", "disk_type": "ssd", "temperature_c": 66}]
            },
            "hosts.temperatures": {
                "hosts": [
                    {"host_id": "qdevice", "sensors": [{"sensor_id": "cpu", "kind": "cpu", "temperature_c": 81}]},
                    {"host_id": "pve", "sensors": [{"sensor_id": "package", "kind": "cpu", "temperature_c": 79}]},
                ]
            },
            "frigate.stats": {"service": {"temperatures": {"apex_0": 74}}},
        }
        return _plain_tool_result(tool_id, payloads[tool_id])

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"thermal.sensors"})

    assert result["created_tasks"] == 3
    incidents = (
        await db_session.execute(select(Incident).where(Incident.watcher_id == "thermal.sensors"))
    ).scalars().all()
    assert len(incidents) == 3
    assert {incident.provider_id for incident in incidents} == {"nutups", "proxmox", "glances"}
    assert {incident.severity for incident in incidents} == {"warning", "critical"}
    assert all(incident.payload["code"] == "temperature_threshold" for incident in incidents)

    run = (await db_session.execute(select(WatcherRun).where(WatcherRun.watcher_id == "thermal.sensors"))).scalar_one()
    assert run.payload["readings_total"] == 8
    assert run.payload["findings_total"] == 3


async def test_thermal_watcher_ignores_unsupported_and_cool_sensors(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        payloads = {
            "nutups.status": {"ups": {"name": "ups", "status": "online", "ups_temperature_c": 30}},
            "opnsense.system.temperature": {
                "temperature": {"sensors": [{"sensor_id": "cpu0", "kind": "cpu", "temperature_c": 50}]}
            },
            "mikrotik.system.health": {
                "health": {"temperature_sensors": [{"sensor_id": "board", "kind": "board", "temperature_c": 45}]}
            },
            "fritzbox.primary.temperature": {"temperature": {"supported": False, "sensors": []}},
            "fritzbox.secondary.temperature": {"temperature": {"supported": False, "sensors": []}},
            "proxmox.disks.temperatures": {"disks": [{"node": "pve", "devpath": "/dev/sda", "temperature_c": 33}]},
            "hosts.temperatures": {"hosts": [{"host_id": "qdevice", "sensors": [{"sensor_id": "cpu", "kind": "cpu", "temperature_c": 55}]}]},
            "frigate.stats": {"service": {"temperatures": {"apex_0": 50}}},
        }
        return _plain_tool_result(tool_id, payloads[tool_id])

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"thermal.sensors"})

    assert result["created_tasks"] == 0
    assert (await db_session.execute(select(func.count()).select_from(Incident))).scalar_one() == 0
    run = (await db_session.execute(select(WatcherRun).where(WatcherRun.watcher_id == "thermal.sensors"))).scalar_one()
    assert run.payload["readings_total"] == 6
    assert run.payload["findings_total"] == 0


async def test_thermal_watcher_does_not_resolve_failed_provider_incident(db_session, monkeypatch):
    incident = Incident(
        dedupe_key="mikrotik-hot",
        watcher_id="thermal.sensors",
        status="open",
        severity="warning",
        provider_id="mikrotik",
        title="MikroTik temperature high",
        description="MikroTik temperature high",
    )
    db_session.add(incident)
    await db_session.flush()

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        if tool_id == "mikrotik.system.health":
            return _failed_tool_result(tool_id)
        if tool_id == "opnsense.system.temperature":
            return _plain_tool_result(
                tool_id,
                {"temperature": {"sensors": [{"sensor_id": "cpu0", "temperature_c": 50}]}},
            )
        return _plain_tool_result(tool_id, {})

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"thermal.sensors"})

    assert result["ok"] is True
    assert incident.status == "open"
    assert incident.missing_runs == 0
    run = (
        await db_session.execute(
            select(WatcherRun).where(WatcherRun.watcher_id == "thermal.sensors")
        )
    ).scalar_one()
    assert "mikrotik" not in run.payload["successful_provider_ids"]
    assert "opnsense" in run.payload["successful_provider_ids"]


async def test_thermal_watcher_fails_when_success_payloads_have_no_readings(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _plain_tool_result(tool_id, {})

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"thermal.sensors"})

    assert result["ok"] is False
    assert result["watchers"][0]["error"] == "no readable thermal sensors"


async def test_thermal_watcher_fails_when_all_providers_fail(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _failed_tool_result(tool_id)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"thermal.sensors"})

    assert result["ok"] is False
    assert result["watchers"][0]["status"] == "error"
    assert result["watchers"][0]["error"]


async def test_watcher_does_not_reopen_operator_handled_incident(db_session, monkeypatch):
    findings = [
        {
            "provider_id": "vps",
            "severity": "critical",
            "message": "1 VPS deploy target(s) unhealthy",
        }
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(findings)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    first = await run_watchers(db_session)
    incident = (await db_session.execute(select(Incident))).scalar_one()
    await resolve_incident_as_handled(
        db_session,
        incident_id=incident.id,
        actor=WATCHER_OPERATOR,
        note="already handled",
    )
    findings[0]["message"] = "2 VPS deploy target(s) unhealthy"
    second = await run_watchers(db_session)

    assert first["created_tasks"] == 1
    assert second["created_tasks"] == 0
    assert second["updated_incidents"] == 1
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 1
    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.status == "resolved"
    assert incident.resolution_reason == "operator_already_handled"
    assert incident.occurrences == 2


async def test_watcher_ignores_ok_findings(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result([{"provider_id": "lab", "severity": "ok", "message": "Everything fine"}])

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session)

    assert result["ok"] is True
    assert result["created_tasks"] == 0
    task_count = (await db_session.execute(select(func.count()).select_from(Task))).scalar_one()
    assert task_count == 0
    run = (
        await db_session.execute(select(WatcherRun).where(WatcherRun.watcher_id == "lab.alerts"))
    ).scalar_one()
    assert run.payload["actionable_total"] == 0


async def test_watcher_respects_min_severity_and_ignore_patterns(db_session, monkeypatch):
    from app.core.settings import get_settings

    monkeypatch.setenv("WATCHERS_MIN_SEVERITY", "critical")
    monkeypatch.setenv("WATCHERS_IGNORE_PATTERNS", "ignore me")
    get_settings.cache_clear()

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(
            [
                {"provider_id": "ha", "severity": "warning", "message": "warning below threshold"},
                {"provider_id": "vps", "severity": "critical", "message": "ignore me for now"},
                {"provider_id": "vps", "severity": "critical", "message": "deploy unhealthy"},
            ]
        )

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session)

    assert result["ok"] is True
    assert result["created_tasks"] == 1
    run = (
        await db_session.execute(select(WatcherRun).where(WatcherRun.watcher_id == "lab.alerts"))
    ).scalar_one()
    assert run.payload["ignored_total"] == 2
    task = (await db_session.execute(select(Task))).scalar_one()
    assert "deploy unhealthy" in task.title


async def test_raising_min_severity_does_not_clear_observed_warning(db_session, monkeypatch):
    findings = [
        {"provider_id": "ha", "severity": "warning", "message": "automation degraded"}
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(findings)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)
    await run_watchers(db_session, watcher_ids={"lab.alerts"})
    incident = (await db_session.execute(select(Incident))).scalar_one()

    await configure_watcher(db_session, "lab.alerts", min_severity="critical")
    await run_watchers(db_session, watcher_ids={"lab.alerts"})

    incident = await db_session.get(Incident, incident.id)
    assert incident.status == "open"
    assert incident.missing_runs == 0
    assert incident.occurrences == 2
    assert incident.payload["policy_state"] == "filtered"
    assert incident.payload["filter_reason"] == "below_min_severity"


async def test_observed_incident_gets_new_task_when_linked_task_is_final(db_session, monkeypatch):
    findings = [
        {"provider_id": "ha", "severity": "warning", "message": "automation degraded"}
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(findings)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)
    await run_watchers(db_session, watcher_ids={"lab.alerts"})
    first = (await db_session.execute(select(Incident))).scalar_one()
    first_task = await db_session.get(Task, first.task_id)
    await set_status(db_session, first_task.id, "claimed", WATCHER_OPERATOR)
    await set_status(db_session, first_task.id, "investigating", WATCHER_OPERATOR)
    await set_status(db_session, first_task.id, "completed", WATCHER_OPERATOR)

    await run_watchers(db_session, watcher_ids={"lab.alerts"})

    incidents = (
        await db_session.execute(select(Incident).order_by(Incident.first_seen_at))
    ).scalars().all()
    assert len(incidents) == 2
    assert incidents[0].status == "resolved"
    assert incidents[0].resolution_reason == "task_finalized"
    assert incidents[1].status == "open"
    assert incidents[1].task_id != first_task.id
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 2


async def test_resolved_root_never_lends_its_final_task_to_new_incident(db_session, monkeypatch):
    finding = {"provider_id": "ha", "severity": "warning", "message": "automation degraded"}

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result([finding])

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)
    await run_watchers(db_session, watcher_ids={"lab.alerts"})
    first = (await db_session.execute(select(Incident))).scalar_one()
    first_task = await db_session.get(Task, first.task_id)
    await set_status(db_session, first_task.id, "claimed", WATCHER_OPERATOR)
    await set_status(db_session, first_task.id, "investigating", WATCHER_OPERATOR)
    await set_status(db_session, first_task.id, "completed", WATCHER_OPERATOR)

    detected = _incident_from_finding(finding)
    result = await _upsert_incident(
        db_session,
        detected,
        actor=WATCHER_OPERATOR,
        root_incident_id=first.id,
    )

    replacement = await db_session.get(Incident, result.incident_id)
    assert first.status == "resolved"
    assert first.resolution_reason == "task_finalized"
    assert replacement.status == "open"
    assert replacement.task_id != first_task.id


async def test_open_dependent_gets_new_task_after_root_clears(db_session):
    old_task = Task(
        title="Resolved upstream fault",
        goal="Investigate upstream fault",
        source="watcher",
        created_by="service:watcher",
    )
    db_session.add(old_task)
    await db_session.flush()
    root = Incident(
        dedupe_key="root-cleared",
        watcher_id="lab.alerts",
        status="resolved",
        severity="critical",
        provider_id="opnsense",
        title="Resolved upstream fault",
        description="Resolved upstream fault",
        task_id=old_task.id,
        resolved_at=datetime.now(UTC),
        resolution_reason="alert_cleared",
    )
    db_session.add(root)
    await db_session.flush()
    detected = _incident_from_finding(
        {
            "provider_id": "homeassistant",
            "severity": "warning",
            "message": "Home Assistant is still unavailable",
        }
    )
    dependent = Incident(
        dedupe_key=detected.dedupe_key,
        watcher_id=detected.watcher_id,
        status="open",
        severity=detected.severity,
        provider_id=detected.provider_id,
        title=detected.title,
        description=detected.description,
        task_id=old_task.id,
        root_cause_incident_id=root.id,
    )
    db_session.add(dependent)
    await db_session.flush()

    result = await _upsert_incident(db_session, detected, actor=WATCHER_OPERATOR)

    replacement = await db_session.get(Incident, result.incident_id)
    assert dependent.status == "resolved"
    assert dependent.resolution_reason == "root_cause_cleared"
    assert replacement.status == "open"
    assert replacement.root_cause_incident_id is None
    assert replacement.task_id != old_task.id
    events = (
        await db_session.execute(
            select(TaskEvent).where(
                TaskEvent.task_id == old_task.id,
                TaskEvent.kind == "watcher.incident.root_cause_cleared",
            )
        )
    ).scalars().all()
    assert len(events) == 1


async def test_watcher_allows_tool_audit_from_separate_postgres_session(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        async with get_session_factory()() as other_db:
            await write_audit(
                other_db,
                actor=actor,
                source=source or "watcher",
                action="tool.run",
                outcome="success",
                tool_id=tool_id,
            )
            await other_db.commit()
        return _tool_result(
            [{"provider_id": "vps", "severity": "critical", "message": "1 VPS deploy target(s) unhealthy"}]
        )

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session)

    assert result["ok"] is True
    assert result["created_tasks"] == 1


async def test_watcher_creates_incident_when_task_finding_fails(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(
            [{"provider_id": "proxmox", "severity": "warning", "message": "1 Proxmox guest(s) not running"}]
        )

    async def broken_add_finding(*args, **kwargs):
        raise SQLAlchemyError("legacy findings schema")

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)
    monkeypatch.setattr("app.services.watchers.add_finding", broken_add_finding)

    result = await run_watchers(db_session)

    assert result["ok"] is True
    assert result["created_tasks"] == 1
    assert (await db_session.execute(select(func.count()).select_from(Task))).scalar_one() == 1
    assert (await db_session.execute(select(func.count()).select_from(Incident))).scalar_one() == 1


async def test_watcher_run_endpoint(client, user, capture_adapter, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(
            [
                {
                    "provider_id": "adguard",
                    "severity": "warning",
                    "message": "AdGuard protection is disabled",
                }
            ]
        )

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)
    _, csrf = await do_login(client, capture_adapter)

    status = await client.get("/api/watchers/status")
    assert status.status_code == 200, status.text
    assert status.json()["watcher_ids"] == [
        "backup.freshness",
        "cloudflare.tunnel",
        "lab.alerts",
        "network.gateway",
        "network.presence",
        "network.wireguard",
        "network.zerotier",
        "power.ups",
        "security.certificates",
        "storage.disks",
        "thermal.sensors",
        "uptimekuma.monitors",
    ]
    assert status.json()["scheduled_watcher_ids"] == [
        "cloudflare.tunnel",
        "lab.alerts",
        "network.gateway",
        "network.wireguard",
        "network.zerotier",
        "power.ups",
        "uptimekuma.monitors",
    ]
    assert status.json()["enabled"] is True
    assert status.json()["interval_seconds"] == 300

    enabled = await client.post(
        "/api/watchers/automation",
        json={"enabled": True},
        headers={"x-csrf-token": csrf},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True

    disabled = await client.post(
        "/api/watchers/automation",
        json={"enabled": False},
        headers={"x-csrf-token": csrf},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False

    response = await client.post("/api/watchers/run", json={}, headers={"x-csrf-token": csrf})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created_tasks"] == 1
    assert body["resolved_incidents"] == 0
    incidents = await client.get("/api/watchers/incidents")
    assert incidents.status_code == 200, incidents.text
    assert incidents.json()[0]["provider_id"] == "adguard"

    async with get_session_factory()() as db:
        stored = await db.get(WatcherAutomationState, "global")
        assert stored is not None
        assert stored.enabled is False
        revision = stored.revision
    reset_runtime_state_for_tests()
    persisted = await client.get("/api/watchers/status")
    assert persisted.status_code == 200
    assert persisted.json()["enabled"] is False
    async with get_session_factory()() as db:
        stored = await db.get(WatcherAutomationState, "global")
        assert stored is not None
        assert stored.revision == revision


async def test_watcher_config_endpoint_updates_per_watcher_runtime_settings(client, user, capture_adapter):
    _, csrf = await do_login(client, capture_adapter)

    response = await client.patch(
        "/api/watchers/config/network.presence",
        json={"enabled": True, "interval_seconds": 600, "min_severity": "critical"},
        headers={"x-csrf-token": csrf},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    row = next(item for item in body["watchers"] if item["id"] == "network.presence")
    assert row["enabled"] is True
    assert row["interval_seconds"] == 600
    assert row["min_severity"] == "critical"
    assert "network.presence" in body["scheduled_watcher_ids"]
    async with get_session_factory()() as db:
        stored = await db.get(WatcherConfig, "network.presence")
        assert stored is not None
        assert stored.enabled is True
        assert stored.interval_seconds == 600
        assert stored.min_severity == "critical"


async def test_operator_resolves_incident_as_already_handled(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(
            [{"provider_id": "adguard", "severity": "warning", "message": "AdGuard protection is disabled"}]
        )

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)
    await run_watchers(db_session)
    incident = (await db_session.execute(select(Incident))).scalar_one()
    task = await db_session.get(Task, incident.task_id)
    db_session.add(TaskCheck(task_id=task.id, description="Verify manually"))
    await db_session.flush()

    resolved = await resolve_incident_as_handled(
        db_session,
        incident_id=incident.id,
        actor=WATCHER_OPERATOR,
        note="already fixed before import",
    )

    assert resolved.status == "resolved"
    assert resolved.resolution_reason == "operator_already_handled"
    task = await db_session.get(Task, incident.task_id)
    assert task.status == "completed"
    assert task.summary.endswith(
        f"Watcher incident {incident.id[:8]} marked already handled by {WATCHER_OPERATOR.audit_id()}."
        " Note: already fixed before import"
    )
    check = (await db_session.execute(select(TaskCheck))).scalar_one()
    assert check.status == "skipped"
    events = (await db_session.execute(select(AuditEvent))).scalars().all()
    assert any(event.action == "watcher.incident.resolve_handled" for event in events)
    status_events = (
        await db_session.execute(
            select(TaskEvent).where(
                TaskEvent.task_id == task.id,
                TaskEvent.kind == "task.status_changed",
            )
        )
    ).scalars().all()
    assert status_events[-1].payload["policy"] == "operator_handled"
    assert status_events[-1].payload["incident_id"] == incident.id

    resolutions = await list_resolutions(db_session, task.id)
    assert len(resolutions) == 1
    assert resolutions[0].incident_id == incident.id
    assert resolutions[0].source == "watcher"


async def test_resolve_incident_handled_endpoint(client, user, capture_adapter, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(
            [{"provider_id": "uptimekuma", "severity": "critical", "message": "Monitor is down"}]
        )

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)
    _, csrf = await do_login(client, capture_adapter)
    await client.post("/api/watchers/run", json={}, headers={"x-csrf-token": csrf})
    incident = (await client.get("/api/watchers/incidents")).json()[0]

    response = await client.post(
        f"/api/watchers/incidents/{incident['id']}/resolve-handled",
        json={"note": "already handled"},
        headers={"x-csrf-token": csrf},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "resolved"
    assert response.json()["resolution_reason"] == "operator_already_handled"
