from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db.models import Incident, Task
from app.tools.execution import ExecutionResult
from tests.conftest import do_login


def _tool_result(findings: list[dict]) -> ExecutionResult:
    now = datetime.now(UTC)
    return ExecutionResult(
        ok=True,
        invocation_id="inv-suite-watch",
        tool_id="lab.alerts.recent",
        started_at=now,
        finished_at=now,
        duration_ms=1,
        result={
            "summary": {
                "provider_id": "lab.alerts",
                "status": "degraded" if findings else "healthy",
                "severity": "warning" if findings else "info",
                "metrics": {"alerts_total": len(findings)},
                "findings": findings,
            }
        },
    )


async def test_suite_4_watcher_incident_operator_flow(client, user, capture_adapter, db_session, monkeypatch):
    findings = [
        {
            "provider_id": "homeassistant",
            "severity": "warning",
            "message": "193 Home Assistant entity/entities unavailable or unknown",
        }
    ]

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        assert actor.kind == "service"
        assert source == "watcher"
        if tool_id == "opnsense.gateways.status":
            return ExecutionResult(
                ok=True,
                invocation_id="inv-gateway",
                tool_id=tool_id,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                duration_ms=1,
                result={"gateways": [], "total": 0, "offline": []},
            )
        if tool_id == "uptimekuma.monitors.status":
            return ExecutionResult(
                ok=True,
                invocation_id="inv-kuma",
                tool_id=tool_id,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                duration_ms=1,
                result={"monitors": [], "total": 0, "by_status": {}},
            )
        if tool_id == "opnsense.wireguard.status":
            return ExecutionResult(
                ok=True,
                invocation_id="inv-wireguard",
                tool_id=tool_id,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                duration_ms=1,
                result={"peers_total": 0, "peers_connected": 0, "peers_stale": []},
            )
        if tool_id == "nutups.status":
            return ExecutionResult(
                ok=True,
                invocation_id="inv-ups",
                tool_id=tool_id,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                duration_ms=1,
                result={
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
            return ExecutionResult(
                ok=True,
                invocation_id="inv-cloudflare",
                tool_id=tool_id,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                duration_ms=1,
                result={"summary": {"metrics": {"tunnels_total": 1, "connections_active": 1}}},
            )
        if tool_id == "zerotier.members.list":
            return ExecutionResult(
                ok=True,
                invocation_id="inv-zerotier",
                tool_id=tool_id,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                duration_ms=1,
                result={"required_total": 1, "required_online": 1, "required_unavailable": 0},
            )
        assert tool_id == "lab.alerts.recent"
        return _tool_result(findings)

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

    first = await client.post("/api/watchers/run", json={}, headers={"x-csrf-token": csrf})
    assert first.status_code == 200, first.text
    assert first.json()["created_tasks"] == 1
    assert first.json()["updated_incidents"] == 0

    second = await client.post("/api/watchers/run", json={}, headers={"x-csrf-token": csrf})
    assert second.status_code == 200, second.text
    assert second.json()["created_tasks"] == 0
    assert second.json()["updated_incidents"] == 1

    findings[0]["message"] = "197 Home Assistant entity/entities unavailable or unknown"
    variable_count = await client.post("/api/watchers/run", json={}, headers={"x-csrf-token": csrf})
    assert variable_count.status_code == 200, variable_count.text
    assert variable_count.json()["created_tasks"] == 0
    assert variable_count.json()["updated_incidents"] == 1

    open_incidents = await client.get("/api/watchers/incidents")
    assert open_incidents.status_code == 200, open_incidents.text
    assert len(open_incidents.json()) == 1
    incident = open_incidents.json()[0]
    assert incident["status"] == "open"
    assert incident["occurrences"] == 3
    assert "197 Home Assistant" in incident["description"]

    handled = await client.post(
        f"/api/watchers/incidents/{incident['id']}/resolve-handled",
        json={"note": "already fixed before watcher review"},
        headers={"x-csrf-token": csrf},
    )
    assert handled.status_code == 200, handled.text
    assert handled.json()["status"] == "resolved"
    assert handled.json()["resolution_reason"] == "operator_already_handled"

    open_after_handled = await client.get("/api/watchers/incidents")
    assert open_after_handled.status_code == 200, open_after_handled.text
    assert open_after_handled.json() == []

    resolved_incidents = await client.get("/api/watchers/incidents?status=resolved")
    assert resolved_incidents.status_code == 200, resolved_incidents.text
    assert len(resolved_incidents.json()) == 1

    repeat_after_handled = await client.post("/api/watchers/run", json={}, headers={"x-csrf-token": csrf})
    assert repeat_after_handled.status_code == 200, repeat_after_handled.text
    assert repeat_after_handled.json()["created_tasks"] == 0
    assert repeat_after_handled.json()["updated_incidents"] == 1

    task_count = (await db_session.execute(select(func.count()).select_from(Task))).scalar_one()
    incident_count = (await db_session.execute(select(func.count()).select_from(Incident))).scalar_one()
    assert task_count == 1
    assert incident_count == 1
