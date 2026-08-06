"""Failure-scenario regression suite (roadmap Phase 2).

Each scenario locks in current watcher/dependency-graph/task behavior so
future changes (e.g. Phase 3's Runbook Engine) can be checked against it,
instead of only being validated against real infrastructure failures.
"""

from sqlalchemy import func, select

from app.core.settings import get_settings
from app.db.models import Incident, Task
from tests.scenario_support import Scenario, Step, export_correlated_cluster, run_scenario


async def test_single_independent_failure_creates_one_task(db_session, monkeypatch):
    scenario = Scenario(
        nodes=[{"id": "vps", "depends_on": []}],
        steps=[
            Step(findings=[{"provider_id": "vps", "severity": "critical", "message": "VPS unreachable"}]),
        ],
    )
    results = await run_scenario(db_session, monkeypatch, scenario)

    assert results[0]["created_tasks"] == 1
    task_count = (await db_session.execute(select(func.count()).select_from(Task))).scalar_one()
    assert task_count == 1
    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.root_cause_incident_id is None


async def test_correlated_multi_provider_outage_creates_one_task(db_session, monkeypatch):
    scenario = Scenario(
        nodes=[
            {"id": "opnsense", "depends_on": []},
            {"id": "proxmox", "depends_on": ["opnsense"]},
            {"id": "homeassistant", "depends_on": ["opnsense"]},
        ],
        steps=[
            Step(
                findings=[
                    {"provider_id": "opnsense", "severity": "critical", "message": "Gateway unreachable"},
                    {"provider_id": "proxmox", "severity": "warning", "message": "Proxmox unreachable"},
                    {"provider_id": "homeassistant", "severity": "warning", "message": "HA unreachable"},
                ]
            ),
        ],
    )
    results = await run_scenario(db_session, monkeypatch, scenario)

    assert results[0]["created_tasks"] == 1
    task_count = (await db_session.execute(select(func.count()).select_from(Task))).scalar_one()
    assert task_count == 1

    incidents = (await db_session.execute(select(Incident))).scalars().all()
    by_provider = {incident.provider_id: incident for incident in incidents}
    assert by_provider["opnsense"].root_cause_incident_id is None
    assert by_provider["proxmox"].root_cause_incident_id == by_provider["opnsense"].id
    assert by_provider["homeassistant"].root_cause_incident_id == by_provider["opnsense"].id


async def test_flap_then_recover(db_session, monkeypatch):
    # Shrink the grace period so this scenario stays short — the exact
    # grace-period math is already covered by test_watchers.py; this
    # scenario only proves the runner can drive a full resolve cycle.
    monkeypatch.setenv("WATCHERS_RESOLVE_AFTER_MISSING_RUNS", "1")
    get_settings.cache_clear()

    scenario = Scenario(
        nodes=[{"id": "adguard", "depends_on": []}],
        steps=[
            Step(findings=[{"provider_id": "adguard", "severity": "warning", "message": "DNS degraded"}]),
            # Same message text keeps the same dedupe_key (only severity
            # escalates) — this is what makes it a refresh, not a new
            # incident, matching test_watchers.py's flap test.
            Step(findings=[{"provider_id": "adguard", "severity": "critical", "message": "DNS degraded"}]),
            Step(findings=[]),  # recovered
        ],
    )
    results = await run_scenario(db_session, monkeypatch, scenario)

    assert results[0]["created_tasks"] == 1
    assert results[1]["updated_incidents"] == 1
    assert results[2]["resolved_incidents"] == 1

    incident = (await db_session.execute(select(Incident))).scalar_one()
    assert incident.status == "resolved"
    assert incident.resolution_reason == "alert_cleared"


async def test_replay_reproduces_correlated_cluster_shape(db_session, monkeypatch):
    # A synthetic "historical" correlated cluster, built directly since no
    # real incidents exist yet on the deployed console. root_cause_incident_id
    # is set the same way watchers.py sets it: flattened to the ultimate root.
    root = Incident(
        dedupe_key="historical-root",
        watcher_id="lab.alerts",
        status="resolved",
        severity="critical",
        provider_id="opnsense",
        title="[Watcher] opnsense: Gateway unreachable",
        description="Gateway unreachable",
        payload={"provider_id": "opnsense", "severity": "critical", "message": "Gateway unreachable"},
    )
    db_session.add(root)
    await db_session.flush()
    dependent = Incident(
        dedupe_key="historical-dependent",
        watcher_id="lab.alerts",
        status="resolved",
        severity="warning",
        provider_id="vps",
        title="[Watcher] vps: VPS unreachable",
        description="VPS unreachable",
        payload={"provider_id": "vps", "severity": "warning", "message": "VPS unreachable"},
        root_cause_incident_id=root.id,
    )
    db_session.add(dependent)
    await db_session.flush()

    findings = await export_correlated_cluster(db_session, root)
    assert {finding["provider_id"] for finding in findings} == {"opnsense", "vps"}

    scenario = Scenario(
        nodes=[
            {"id": "opnsense", "depends_on": []},
            {"id": "vps", "depends_on": ["opnsense"]},
        ],
        steps=[Step(findings=findings)],
    )
    results = await run_scenario(db_session, monkeypatch, scenario)

    assert results[0]["created_tasks"] == 1
    replayed = (
        await db_session.execute(select(Incident).where(Incident.dedupe_key.notin_(["historical-root", "historical-dependent"])))
    ).scalars().all()
    assert len(replayed) == 2
    by_provider = {incident.provider_id: incident for incident in replayed}
    assert by_provider["opnsense"].root_cause_incident_id is None
    assert by_provider["vps"].root_cause_incident_id == by_provider["opnsense"].id
    assert by_provider["vps"].severity == "warning"
