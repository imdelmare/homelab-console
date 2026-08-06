"""Reusable failure-scenario runner for watcher/dependency-graph tests.

Formalizes the pattern already used across test_watchers.py (a mutable
findings list captured by a fake execute_tool, mutated between repeated
run_watchers() calls to advance a scenario) into a named, reusable
abstraction, so scenarios can be authored once and reused across test
files instead of duplicating the monkeypatch boilerplate per test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Incident
from app.services import dependency_graph, inventory
from app.services.watchers import run_watchers
from app.tools.execution import ExecutionResult


@dataclass(frozen=True)
class Step:
    findings: list[dict]


@dataclass(frozen=True)
class Scenario:
    nodes: list[dict]  # dependency_graph topology, DependencyEntry shape
    steps: list[Step]


def export_incident_as_finding(incident: Incident) -> dict:
    """Return the original redacted, JSON-safe watcher finding."""
    return dict(incident.payload)


async def export_correlated_cluster(db: AsyncSession, incident: Incident) -> list[dict]:
    """Export a root incident and its correlated dependents for replay tests."""
    root_id = incident.root_cause_incident_id or incident.id
    root = incident if incident.id == root_id else await db.get(Incident, root_id)
    if root is None:
        return [export_incident_as_finding(incident)]

    dependents = list(
        (
            await db.execute(
                select(Incident).where(
                    Incident.root_cause_incident_id == root.id,
                    Incident.id != root.id,
                )
            )
        ).scalars()
    )
    return [export_incident_as_finding(root)] + [
        export_incident_as_finding(dependent) for dependent in dependents
    ]


def _tool_result(findings: list[dict]) -> ExecutionResult:
    now = datetime.now(UTC)
    return ExecutionResult(
        ok=True,
        invocation_id="inv-scenario",
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


async def run_scenario(db_session, monkeypatch, scenario: Scenario) -> list[dict]:
    """Configure the dependency graph for `scenario`, then drive
    run_watchers() once per step with that step's findings, returning each
    step's result dict."""
    entries = [inventory.DependencyEntry(**item) for item in scenario.nodes]
    monkeypatch.setattr(inventory, "list_dependencies", lambda: entries)
    dependency_graph.clear_cache()

    current: list[dict] = []

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        return _tool_result(current)

    monkeypatch.setattr("app.services.watchers.execute_tool", fake_execute_tool)

    results = []
    for step in scenario.steps:
        current[:] = step.findings
        results.append(await run_watchers(db_session))
    return results
