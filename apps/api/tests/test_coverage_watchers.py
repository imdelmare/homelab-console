"""Tests for the coverage watchers: backup freshness, disk health and TLS
certificate expiry, plus the summary findings that feed lab.alerts."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.db.models import Incident, Task, WatcherRun
from app.services import inventory
from app.services.watchers import (
    _backup_freshness_incidents,
    _certificate_incidents,
    _disk_health_incidents,
    _notification_group_key,
    run_watchers,
)
from app.tools.execution import ExecutionError, ExecutionResult


@pytest.fixture(autouse=True)
def disable_task_router_for_watcher_tests(monkeypatch):
    async def fake_enqueue_task_routing(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.watchers.enqueue_task_routing", fake_enqueue_task_routing)


def _tool_result(tool_id: str, result: dict | None, *, ok: bool = True) -> ExecutionResult:
    now = datetime.now(UTC)
    return ExecutionResult(
        ok=ok,
        invocation_id="inv-coverage",
        tool_id=tool_id,
        started_at=now,
        finished_at=now,
        duration_ms=1,
        result=result if ok else None,
        error=None if ok else ExecutionError(code="provider_error", message="tool failed"),
    )


def _patch_provider_config(monkeypatch, by_provider: dict[str, dict]):
    monkeypatch.setattr(
        inventory, "provider_config", lambda provider_id: by_provider.get(provider_id, {})
    )


# ---------------------------------------------------------------------------
# Detector units


def test_backup_freshness_flags_stale_and_missing_guests(monkeypatch):
    _patch_provider_config(
        monkeypatch,
        {
            "proxmox": {
                "backup_max_age_days": 3,
                "backup_ignore_vmids": [300],
                "required_backup_vmids": [200],
            }
        },
    )
    payload = {
        "backups_by_guest": [
            {"vmid": 100, "latest_age_days": 1.0},
            {"vmid": 101, "latest_age_days": 5.0},
            {"vmid": 102, "latest_age_days": 8.0},
            {"vmid": 300, "latest_age_days": 30.0},
        ]
    }

    incidents = _backup_freshness_incidents(payload, {})

    by_basis = {item.dedupe_basis: item for item in incidents}
    assert set(by_basis) == {"backup_stale:101", "backup_stale:102", "backup_missing:200"}
    assert by_basis["backup_stale:101"].severity == "warning"
    assert by_basis["backup_stale:102"].severity == "critical"
    assert by_basis["backup_missing:200"].severity == "warning"


def test_required_guest_is_satisfied_by_matching_pbs_group(monkeypatch):
    _patch_provider_config(
        monkeypatch,
        {"proxmox": {"required_backup_vmids": [100, 101]}},
    )
    pbs_payload = {
        "backup_groups": [
            {
                "store": "main",
                "backup_type": "vm",
                "backup_id": "100",
                "latest_backup_at": datetime.now(UTC).timestamp(),
            }
        ]
    }

    incidents = _backup_freshness_incidents({}, pbs_payload)

    by_basis = {item.dedupe_basis: item for item in incidents}
    assert "backup_missing:100" not in by_basis
    assert "backup_missing:101" in by_basis


def test_backup_freshness_flags_stale_pbs_groups(monkeypatch):
    _patch_provider_config(
        monkeypatch,
        {"pbs": {"backup_group_max_age_days": 3, "backup_ignore_groups": ["vm/900"]}},
    )
    now = datetime.now(UTC).timestamp()
    payload = {
        "backup_groups": [
            {"store": "main", "backup_type": "vm", "backup_id": "100", "latest_backup_at": now - 86400},
            {"store": "main", "backup_type": "vm", "backup_id": "101", "latest_backup_at": now - 5 * 86400},
            {"store": "main", "backup_type": "ct", "backup_id": "102", "latest_backup_at": now - 10 * 86400},
            {"store": "main", "backup_type": "vm", "backup_id": "900", "latest_backup_at": now - 30 * 86400},
            {"store": "main", "backup_type": "vm", "backup_id": "103", "latest_backup_at": None},
        ]
    }

    incidents = _backup_freshness_incidents({}, payload)

    by_basis = {item.dedupe_basis: item for item in incidents}
    assert set(by_basis) == {
        "pbs_backup_stale:main:vm/101",
        "pbs_backup_stale:main:ct/102",
    }
    assert by_basis["pbs_backup_stale:main:vm/101"].severity == "warning"
    assert by_basis["pbs_backup_stale:main:ct/102"].severity == "critical"


def test_disk_health_flags_only_failing_disks():
    payload = {
        "disks": [
            {"node": "pve1", "devpath": "/dev/sda", "health": "PASSED"},
            {"node": "pve1", "devpath": "/dev/sdb", "health": "UNKNOWN"},
            {"node": "pve1", "devpath": "/dev/sdc", "health": ""},
            {"node": "pve2", "devpath": "/dev/nvme0n1", "disk_model": "WD SN770", "health": "FAILED"},
        ]
    }

    incidents = _disk_health_incidents(payload)

    assert len(incidents) == 1
    assert incidents[0].severity == "critical"
    assert incidents[0].dedupe_basis == "disk_health:pve2:/dev/nvme0n1"
    assert "FAILED" in incidents[0].description


def test_notification_group_uses_topology_root(monkeypatch):
    from app.services.watchers import DetectedIncident

    monkeypatch.setattr(
        "app.services.watchers.dependency_graph.upstream_of",
        lambda provider_id: {
            "vps": ["wireguard_tunnel", "opnsense"],
            "wireguard_tunnel": ["opnsense"],
            "opnsense": [],
        }.get(provider_id, []),
    )
    incident = DetectedIncident(
        watcher_id="lab.alerts",
        dedupe_key="vps-down",
        dedupe_basis="vps-down",
        severity="critical",
        provider_id="vps",
        title="VPS unreachable",
        description="VPS unreachable",
        payload={"code": "provider_health"},
    )

    assert _notification_group_key(incident) == "topology:opnsense"


def test_lab_alert_non_connectivity_finding_is_not_topology_grouped():
    from app.services.watchers import DetectedIncident

    incident = DetectedIncident(
        watcher_id="lab.alerts",
        dedupe_key="guest-stopped",
        dedupe_basis="guests_not_running",
        severity="warning",
        provider_id="proxmox",
        title="Guest stopped",
        description="Guest stopped",
        payload={"code": "guests_not_running"},
    )

    assert _notification_group_key(incident) == ""


def test_certificate_incidents_threshold_bands():
    payload = {
        "certificates": [
            {"id": "expired", "days_until_expiry": -2.0, "warning_days": 21, "critical_days": 7},
            {"id": "critical", "days_until_expiry": 5.0, "warning_days": 21, "critical_days": 7},
            {"id": "warning", "days_until_expiry": 15.0, "warning_days": 21, "critical_days": 7},
            {"id": "healthy", "days_until_expiry": 100.0, "warning_days": 21, "critical_days": 7},
            {"id": "unreachable", "days_until_expiry": None, "warning_days": 21, "critical_days": 7},
        ]
    }

    incidents = _certificate_incidents(payload)

    by_basis = {item.dedupe_basis: item for item in incidents}
    assert set(by_basis) == {
        "cert_expiry:expired",
        "cert_expiry:critical",
        "cert_expiry:warning",
    }
    assert by_basis["cert_expiry:expired"].severity == "critical"
    assert "expired" in by_basis["cert_expiry:expired"].description
    assert by_basis["cert_expiry:critical"].severity == "critical"
    assert by_basis["cert_expiry:warning"].severity == "warning"


def test_certificate_incidents_respects_zero_critical_days():
    incidents = _certificate_incidents(
        {
            "certificates": [
                {
                    "id": "warning-only",
                    "days_until_expiry": 3.0,
                    "warning_days": 21,
                    "critical_days": 0,
                }
            ]
        }
    )

    assert len(incidents) == 1
    assert incidents[0].severity == "warning"


# ---------------------------------------------------------------------------
# Watcher runs


async def test_backup_watcher_tolerates_missing_pbs_and_dedupes(db_session, monkeypatch):
    _patch_provider_config(monkeypatch, {"proxmox": {"backup_max_age_days": 3}})

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        assert source == "watcher"
        if tool_id == "proxmox.backups.list":
            return _tool_result(tool_id, {"backups_by_guest": [{"vmid": 101, "latest_age_days": 5.0}]})
        assert tool_id == "pbs.backup.jobs.health"
        return _tool_result(tool_id, None, ok=False)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    first = await run_watchers(db_session, watcher_ids={"backup.freshness"})
    second = await run_watchers(db_session, watcher_ids={"backup.freshness"})

    assert first["ok"] is True
    assert first["created_tasks"] == 1
    assert second["created_tasks"] == 0
    assert second["updated_incidents"] == 1
    run_payload = first["watchers"][0]["payload"]
    assert run_payload["errors"] == {"pbs.backup.jobs.health": "tool failed"}

    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.watcher_id == "backup.freshness"
    assert incident.provider_id == "proxmox"
    assert incident.status == "open"
    assert incident.task_id


async def test_backup_watcher_errors_when_no_source_is_readable(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(tool_id, None, ok=False)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"backup.freshness"})

    assert result["ok"] is False
    assert result["watchers"][0]["status"] == "error"
    task_count = (await db_session.execute(select(func.count()).select_from(Task))).scalar_one()
    assert task_count == 0


async def test_backup_watcher_does_not_clear_incidents_for_failed_source(
    db_session, monkeypatch
):
    _patch_provider_config(
        monkeypatch,
        {
            "proxmox": {"backup_max_age_days": 3},
            "pbs": {"backup_group_max_age_days": 3},
        },
    )
    now = datetime.now(UTC).timestamp()
    pbs_available = True

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        if tool_id == "proxmox.backups.list":
            return _tool_result(tool_id, {"backups_by_guest": []})
        if pbs_available:
            return _tool_result(
                tool_id,
                {
                    "backup_groups": [
                        {
                            "store": "main",
                            "backup_type": "vm",
                            "backup_id": "101",
                            "latest_backup_at": now - 5 * 86400,
                        }
                    ]
                },
            )
        return _tool_result(tool_id, None, ok=False)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    await run_watchers(db_session, watcher_ids={"backup.freshness"})
    pbs_available = False
    await run_watchers(db_session, watcher_ids={"backup.freshness"})
    await run_watchers(db_session, watcher_ids={"backup.freshness"})

    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.provider_id == "pbs"
    assert incident.status == "open"
    assert incident.missing_runs == 0


async def test_certificate_watcher_creates_task(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        assert tool_id == "network.tls.certificates"
        return _tool_result(
            tool_id,
            {
                "certificates": [
                    {
                        "id": "vps_api",
                        "name": "VPS API",
                        "days_until_expiry": 4.0,
                        "warning_days": 21,
                        "critical_days": 7,
                    }
                ]
            },
        )

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"security.certificates"})

    assert result["ok"] is True
    assert result["created_tasks"] == 1
    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.watcher_id == "security.certificates"
    assert incident.severity == "critical"
    assert "VPS API" in incident.description


async def test_disk_watcher_records_readings_and_flags_failure(db_session, monkeypatch):
    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        assert tool_id == "proxmox.disks.temperatures"
        return _tool_result(
            tool_id,
            {
                "disks": [
                    {"node": "pve1", "devpath": "/dev/sda", "disk_model": "ST4000", "health": "PASSED", "wearout": None},
                    {"node": "pve1", "devpath": "/dev/nvme0n1", "disk_model": "SN770", "health": "FAILED", "wearout": 55},
                ]
            },
        )

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    result = await run_watchers(db_session, watcher_ids={"storage.disks"})

    assert result["ok"] is True
    assert result["created_tasks"] == 1
    run = (await db_session.execute(select(WatcherRun))).scalar_one()
    assert run.payload["readings_total"] == 2
    assert {item["devpath"] for item in run.payload["readings"]} == {"/dev/sda", "/dev/nvme0n1"}
    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.watcher_id == "storage.disks"
    assert incident.severity == "critical"


# ---------------------------------------------------------------------------
# Summary findings feeding lab.alerts


async def test_proxmox_summary_flags_hot_nodes(monkeypatch):
    from app.tools import summaries

    async def fake_health(_provider_id):
        return {"status": "healthy", "detail": ""}

    async def fake_guests():
        return {"guests": []}

    async def fake_storage():
        return {"storage": []}

    async def fake_failed():
        return {"failed_tasks": []}

    async def fake_nodes():
        return {
            "nodes": [
                {
                    "node": "pve1",
                    "status": "online",
                    "cpu_usage": 0.92,
                    "memory_used_bytes": 30 * 1024**3,
                    "memory_total_bytes": 32 * 1024**3,
                },
                {
                    "node": "pve2",
                    "status": "online",
                    "cpu_usage": 0.10,
                    "memory_used_bytes": 8 * 1024**3,
                    "memory_total_bytes": 32 * 1024**3,
                },
                {"node": "pve3", "status": "offline"},
            ]
        }

    monkeypatch.setattr(summaries, "_health", fake_health)
    monkeypatch.setattr(summaries.proxmox_tools, "guests_list", fake_guests)
    monkeypatch.setattr(summaries.proxmox_tools, "storage_list", fake_storage)
    monkeypatch.setattr(summaries.proxmox_tools, "tasks_failed", fake_failed)
    monkeypatch.setattr(summaries.proxmox_tools, "nodes_list", fake_nodes)
    monkeypatch.setattr(summaries, "provider_config", lambda _pid: {})

    result = await summaries.proxmox_summary()

    summary = result["summary"]
    codes = {item.get("code") for item in summary["findings"]}
    assert {"nodes_offline", "node_cpu_high", "node_memory_high"} <= codes
    assert summary["metrics"]["nodes_total"] == 3
    assert summary["metrics"]["nodes_online"] == 2
    assert summary["metrics"]["nodes_cpu_high"] == 1
    assert summary["metrics"]["nodes_memory_high"] == 1
    by_code = {item.get("code"): item for item in summary["findings"]}
    assert "pve1" in by_code["node_cpu_high"]["message"]
    assert "pve3" in by_code["nodes_offline"]["message"]


async def test_proxmox_summary_ignores_expected_guest_and_old_failed_tasks(monkeypatch):
    from app.tools import summaries

    now = int(datetime.now(UTC).timestamp())

    async def fake_health(_provider_id):
        return {"status": "healthy", "detail": ""}

    async def fake_guests():
        return {
            "guests": [
                {"vmid": 111, "name": "native-console", "status": "stopped"},
                {"vmid": 222, "name": "unexpected", "status": "stopped"},
            ]
        }

    async def fake_storage():
        return {"storage": []}

    async def fake_failed():
        return {
            "failed_tasks": [
                {"started_at": now - 2 * 3600},
                {"started_at": now - 48 * 3600},
                {"started_at": None},
            ]
        }

    async def fake_nodes():
        return {"nodes": []}

    monkeypatch.setattr(summaries, "_health", fake_health)
    monkeypatch.setattr(summaries.proxmox_tools, "guests_list", fake_guests)
    monkeypatch.setattr(summaries.proxmox_tools, "storage_list", fake_storage)
    monkeypatch.setattr(summaries.proxmox_tools, "tasks_failed", fake_failed)
    monkeypatch.setattr(summaries.proxmox_tools, "nodes_list", fake_nodes)
    monkeypatch.setattr(
        summaries,
        "provider_config",
        lambda _pid: {
            "ignored_stopped_vmids": [111],
            "failed_task_warning_threshold": 1,
            "failed_task_max_age_hours": 24,
        },
    )

    summary = (await summaries.proxmox_summary())["summary"]
    findings = {item["code"]: item["message"] for item in summary["findings"]}

    assert findings["guests_not_running"] == "1 Proxmox guest(s) not running"
    assert findings["recent_failed_tasks"] == (
        "1 Proxmox task(s) failed in the last 24 hour(s)"
    )
    assert summary["metrics"]["guests_stopped_ignored"] == 1
    assert summary["metrics"]["failed_tasks"] == 1


async def test_frigate_summary_flags_stalled_cameras(monkeypatch):
    from app.tools import summaries

    async def fake_health(_provider_id):
        return {"status": "healthy", "detail": ""}

    async def fake_stats():
        return {"service": {"detection_fps": 5.0}, "cameras": [], "detectors": []}

    async def fake_config_summary():
        return {
            "config": {"cameras_total": 3, "safe_mode": False},
            "cameras": [
                {"name": "porch", "enabled": True, "camera_fps": 5.0},
                {"name": "garage", "enabled": True, "camera_fps": 0.0},
                {"name": "garden", "enabled": False, "camera_fps": None},
            ],
        }

    monkeypatch.setattr(summaries, "_health", fake_health)
    monkeypatch.setattr(summaries.frigate_tools, "stats", fake_stats)
    monkeypatch.setattr(summaries.frigate_tools, "config_summary", fake_config_summary)

    result = await summaries.frigate_summary()

    summary = result["summary"]
    by_code = {item.get("code"): item for item in summary["findings"]}
    assert "garage" in by_code["cameras_stalled"]["message"]
    assert "garden" in by_code["cameras_disabled"]["message"]
    assert summary["metrics"]["cameras_stalled"] == 1
    assert summary["metrics"]["cameras_disabled"] == 1


async def test_lab_security_summary_reports_empty_tls_coverage(monkeypatch):
    from app.tools import summaries

    async def fake_health(_provider_id):
        return {"status": "healthy", "detail": ""}

    async def healthy_summary():
        return {"summary": {"provider_id": "test", "metrics": {}, "findings": []}}

    async def empty_certificates():
        return {"certificates": [], "unreachable": [], "expiring": [], "total": 0}

    monkeypatch.setattr(summaries, "_health", fake_health)
    monkeypatch.setattr(summaries, "opnsense_summary", healthy_summary)
    monkeypatch.setattr(summaries, "adguard_summary", healthy_summary)
    monkeypatch.setattr(summaries, "nextcloud_summary", healthy_summary)
    monkeypatch.setattr(summaries, "uptimekuma_summary", healthy_summary)
    monkeypatch.setattr(summaries.netcheck, "tls_certificates", empty_certificates)

    result = await summaries.lab_security_summary()

    summary = result["summary"]
    assert summary["status"] == "degraded"
    assert summary["metrics"]["tls_targets_total"] == 0
    assert {item.get("code") for item in summary["findings"]} >= {"tls_coverage_empty"}


async def test_lab_storage_summary_includes_backup_and_disk_health(monkeypatch):
    from app.tools import summaries

    async def fake_health(_provider_id):
        return {"status": "healthy", "detail": ""}

    async def proxmox_summary():
        return {"summary": {"provider_id": "proxmox", "metrics": {}, "findings": []}}

    async def pbs_summary():
        return {
            "summary": {
                "provider_id": "pbs",
                "metrics": {
                    "backup_groups_total": 3,
                    "backup_groups_stale": 2,
                    "backup_oldest_age_days": 8.0,
                },
                "findings": [{"severity": "warning", "message": "stale", "code": "backup_groups_stale"}],
            }
        }

    async def neutral_summary():
        return {"summary": {"provider_id": "neutral", "metrics": {}, "findings": []}}

    async def disks():
        return {
            "disks": [
                {"health": "PASSED", "wearout": 80},
                {"health": "FAILED", "wearout": 10},
            ]
        }

    async def backups():
        return {"backups_by_guest": [{"vmid": 100}]}

    monkeypatch.setattr(summaries, "_health", fake_health)
    monkeypatch.setattr(summaries, "proxmox_summary", proxmox_summary)
    monkeypatch.setattr(summaries, "pbs_summary", pbs_summary)
    monkeypatch.setattr(summaries, "nextcloud_summary", neutral_summary)
    monkeypatch.setattr(summaries, "nutups_summary", neutral_summary)
    monkeypatch.setattr(summaries.proxmox_tools, "disks_temperatures", disks)
    monkeypatch.setattr(summaries.proxmox_tools, "backups_list", backups)
    monkeypatch.setattr(summaries, "provider_config", lambda _pid: {})

    result = await summaries.lab_storage_summary()

    summary = result["summary"]
    metrics = summary["metrics"]
    assert metrics["proxmox_disks_total"] == 2
    assert metrics["proxmox_disks_unhealthy"] == 1
    assert metrics["proxmox_disks_wearout_low"] == 1
    assert metrics["proxmox_backup_guests"] == 1
    assert metrics["pbs_backup_groups_stale"] == 2
    codes = {item.get("code") for item in summary["findings"]}
    assert {"backup_groups_stale", "disk_health_failed", "disk_wearout_low"} <= codes


async def test_lab_automation_summary_propagates_stalled_cameras(monkeypatch):
    from app.tools import summaries

    async def fake_health(_provider_id):
        return {"status": "healthy", "detail": ""}

    async def neutral_summary():
        return {"summary": {"provider_id": "neutral", "metrics": {}, "findings": []}}

    async def frigate_summary():
        return {
            "summary": {
                "provider_id": "frigate",
                "metrics": {"cameras_total": 4, "cameras_disabled": 0, "cameras_stalled": 2},
                "findings": [],
            }
        }

    monkeypatch.setattr(summaries, "_health", fake_health)
    monkeypatch.setattr(summaries, "homeassistant_summary", neutral_summary)
    monkeypatch.setattr(summaries, "frigate_summary", frigate_summary)
    monkeypatch.setattr(summaries, "emqx_summary", neutral_summary)

    result = await summaries.lab_automation_summary()

    assert result["summary"]["metrics"]["frigate_cameras_stalled"] == 2
