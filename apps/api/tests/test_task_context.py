from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import cast

import pytest
from sqlalchemy import select

from app.db.models import Incident, Task, TaskEvent, ToolInvocation
from app.domain.actors import Actor
from app.services import dependency_graph, inventory, runbooks
from app.services.task_context import (
    DEFAULT_BUDGET,
    _incident_type,
    _providers_from_text,
    compile_task_context,
)
from app.services.tasks_service import create_task
from tests.scenario_support import Scenario, Step, run_scenario

OPERATOR = Actor(kind="user", id="operator", label="operator")


class _FakeIncident:
    def __init__(self, watcher_id: str) -> None:
        self.watcher_id = watcher_id


@pytest.mark.parametrize(
    "watcher_id,text,expected",
    [
        ("lab.alerts", "monitor is down for console", "monitor_down"),
        ("lab.alerts", "gateway wan latency high", "gateway_alert"),
        ("lab.alerts", "wireguard tunnel dropped", "connectivity_alert"),
        ("lab.alerts", "backup datastore near full", "backup_alert"),
        ("lab.alerts", "ups on battery", "power_alert"),
        # lab.alerts incident whose text matches none of the watcher-specific
        # branches falls through to the generic classifiers below.
        ("lab.alerts", "adguard dns resolution failing", "dns_alert"),
        ("lab.alerts", "totally unrelated text", "task_handoff"),
        # Non-lab.alerts incidents skip the watcher-specific branches entirely.
        ("other.watcher", "gateway wan latency high", "task_handoff"),
        ("other.watcher", "nextcloud unreachable", "availability_alert"),
    ],
)
def test_incident_type_classification(watcher_id, text, expected):
    assert _incident_type(cast(Incident, _FakeIncident(watcher_id)), text) == expected


def test_incident_type_without_incident_uses_generic_classifiers():
    assert _incident_type(None, "dns lookups failing") == "dns_alert"
    assert _incident_type(None, "ups battery low") == "power_alert"
    assert _incident_type(None, "host unreachable") == "availability_alert"
    assert _incident_type(None, "nothing relevant") == "task_handoff"


def test_providers_from_text_matches_direct_and_keyword_derived():
    providers = _providers_from_text("cloudflare tunnel and opnsense gateway with dns issues")
    assert set(providers) == {"cloudflaretunnel", "opnsense", "adguard"}


def test_providers_from_text_no_match_returns_empty():
    assert _providers_from_text("nothing about any provider here") == []


async def test_compile_task_context_without_incident(db_session):
    task = await create_task(db_session, "Investigate", "Something odd", OPERATOR)
    await db_session.flush()

    context = await compile_task_context(db_session, task.id)

    assert context["incident"] == {"type": "task_handoff"}
    assert context["provider_ids"] == []
    assert "no linked incident" in context["brief"]
    assert context["budget"] == DEFAULT_BUDGET


async def test_recommended_tools_keyword_only_fallback(db_session):
    task = await create_task(db_session, "Investigate tunnel", "tunnel seems flaky", OPERATOR)
    await db_session.flush()

    context = await compile_task_context(db_session, task.id)

    tool_ids = [tool["tool_id"] for tool in context["recommended_tools"]]
    # "tunnel" alone doesn't match any PROVIDER_TOOL_ALLOWLIST provider (only
    # "cloudflare" or "gateway"/"wireguard" do), so every recommendation here
    # must come from KEYWORD_TOOL_ALLOWLIST["tunnel"].
    assert tool_ids
    assert set(tool_ids).issubset(
        {"cloudflare.summary", "cloudflare.tunnels.status", "vps.wireguard.status"}
    )


async def test_recommended_tools_temperature_keyword_uses_generic_fallback(db_session):
    task = await create_task(db_session, "Temperature check", "controlla sensori temperatura apparati", OPERATOR)
    await db_session.flush()

    context = await compile_task_context(db_session, task.id)

    assert [tool["tool_id"] for tool in context["recommended_tools"]] == [
        "lab.summary",
        "lab.alerts.recent",
    ]


async def test_recommended_tools_falls_back_to_lab_summary_when_nothing_matches(db_session):
    task = await create_task(db_session, "Investigate", "no keywords or providers here", OPERATOR)
    await db_session.flush()

    context = await compile_task_context(db_session, task.id)

    assert [tool["tool_id"] for tool in context["recommended_tools"]] == [
        "lab.summary",
        "lab.alerts.recent",
    ]


async def test_provider_context_event_drives_context_without_incident(db_session, monkeypatch):
    _configure_runbook(monkeypatch, _GATEWAY_RUNBOOK_CONFIG)
    task = await create_task(db_session, "Provider check", "operator opened provider task", OPERATOR, source="provider")
    await db_session.flush()
    db_session.add(
        TaskEvent(
            task_id=task.id,
            kind="task.provider_context",
            payload={
                "provider_id": "opnsense",
                "status": "degraded",
                "detail": "gateway latency high",
                "runbook_hint": "gateway_alert / connectivity_alert",
            },
        )
    )
    await db_session.flush()

    context = await compile_task_context(db_session, task.id)

    assert context["incident"] == {"type": "gateway_alert"}
    assert context["provider_ids"] == ["opnsense"]
    assert [tool["tool_id"] for tool in context["recommended_tools"]][:2] == [
        "opnsense.summary",
        "opnsense.gateways.status",
    ]
    assert "Following runbook 'Gateway alert'" in context["brief"]


async def test_thermal_incident_recommends_provider_summary_not_raw_temperature_tool(db_session):
    task = await create_task(db_session, "Thermal alert", "OPNsense temperature is high", OPERATOR)
    await db_session.flush()
    db_session.add(
        Incident(
            dedupe_key="thermal-opnsense",
            watcher_id="thermal.sensors",
            status="open",
            severity="warning",
            provider_id="opnsense",
            title="[Thermal Watcher] OPNsense cpu0 temperature is 80 C",
            description="OPNsense cpu0 temperature is 80 C",
            task_id=task.id,
        )
    )
    await db_session.flush()

    context = await compile_task_context(db_session, task.id)

    tool_ids = [tool["tool_id"] for tool in context["recommended_tools"]]
    assert tool_ids[:2] == ["opnsense.summary", "opnsense.gateways.status"]
    assert "opnsense.system.temperature" not in tool_ids


def _configure_graph(monkeypatch, nodes: list[dict]) -> None:
    entries = [inventory.DependencyEntry(**item) for item in nodes]
    monkeypatch.setattr(inventory, "list_dependencies", lambda: entries)
    dependency_graph.clear_cache()


async def test_brief_cites_dependency_chain_for_correlated_incident(db_session, monkeypatch):
    _configure_graph(
        monkeypatch,
        [
            {"id": "opnsense", "label": "OPNsense", "depends_on": []},
            {
                "id": "wireguard_tunnel",
                "label": "WireGuard tunnel",
                "kind": "path",
                "depends_on": ["opnsense"],
            },
            {"id": "vps", "label": "VPS", "depends_on": ["wireguard_tunnel"]},
        ],
    )
    task = await create_task(db_session, "Investigate", "goal placeholder", OPERATOR)
    await db_session.flush()

    now = datetime.now(UTC)
    root = Incident(
        dedupe_key="root-key",
        watcher_id="lab.alerts",
        status="open",
        severity="critical",
        provider_id="opnsense",
        title="OPNsense gateway unreachable",
        description="gateway down",
        task_id=task.id,
        last_seen_at=now - timedelta(minutes=5),
    )
    db_session.add(root)
    await db_session.flush()

    dependent = Incident(
        dedupe_key="dependent-key",
        watcher_id="lab.alerts",
        status="open",
        severity="warning",
        provider_id="vps",
        title="VPS unreachable",
        description="vps unreachable",
        task_id=task.id,
        root_cause_incident_id=root.id,
        last_seen_at=now,
    )
    db_session.add(dependent)
    await db_session.flush()

    context = await compile_task_context(db_session, task.id)

    assert "OPNsense → WireGuard tunnel → VPS" in context["brief"]
    assert root.title in context["brief"]
    assert set(context["provider_ids"]) >= {"opnsense", "vps"}


async def test_recommended_tools_capped_at_budget(db_session):
    task = await create_task(db_session, "Investigate", "goal placeholder", OPERATOR)
    await db_session.flush()
    db_session.add(
        Incident(
            dedupe_key="incident-key",
            watcher_id="lab.alerts",
            status="open",
            severity="critical",
            provider_id="opnsense",
            title="gateway wireguard tunnel backup datastore trouble",
            description="opnsense mikrotik pbs proxmox nextcloud adguard cloudflare dns",
            task_id=task.id,
        )
    )
    await db_session.flush()

    context = await compile_task_context(db_session, task.id)

    assert len(context["recommended_tools"]) == DEFAULT_BUDGET["max_tool_calls"]
    # Every tool_id present is unique: _dedupe must run before the cap is applied.
    tool_ids = [tool["tool_id"] for tool in context["recommended_tools"]]
    assert len(tool_ids) == len(set(tool_ids))


def _configure_runbook(monkeypatch, raw: dict) -> None:
    # Wrapping the replacement in lru_cache too keeps a working
    # .cache_clear(), since conftest.py's teardown calls runbooks.clear_cache()
    # (which unconditionally calls _load_raw.cache_clear()) before this
    # test's monkeypatch has been undone.
    monkeypatch.setattr(runbooks, "_load_raw", lru_cache(lambda: raw))
    runbooks.clear_cache()


_GATEWAY_RUNBOOK_CONFIG = {
    "runbooks": [
        {
            "incident_type": "gateway_alert",
            "label": "Gateway alert",
            "steps": [
                {"tool_id": "opnsense.summary", "evidence": "Check gateway summary first."},
                {"tool_id": "opnsense.gateways.status", "evidence": "Check individual gateway health."},
                {"tool_id": "opnsense.interfaces.statistics", "evidence": "Check interface counters."},
            ],
            "escalation_note": "Ask the operator before further action.",
        }
    ]
}


async def _gateway_alert_task(db_session) -> str:
    task = await create_task(db_session, "Investigate", "gateway trouble", OPERATOR)
    await db_session.flush()
    db_session.add(
        Incident(
            dedupe_key="gateway-key",
            watcher_id="lab.alerts",
            status="open",
            severity="critical",
            provider_id="opnsense",
            title="gateway down",
            description="gateway down",
            task_id=task.id,
        )
    )
    await db_session.flush()
    return task.id


async def test_matched_runbook_drives_recommended_tools_in_order(db_session, monkeypatch):
    _configure_runbook(monkeypatch, _GATEWAY_RUNBOOK_CONFIG)
    task_id = await _gateway_alert_task(db_session)

    context = await compile_task_context(db_session, task_id)

    assert [tool["tool_id"] for tool in context["recommended_tools"]] == [
        "opnsense.summary",
        "opnsense.gateways.status",
        "opnsense.interfaces.statistics",
    ]
    assert context["recommended_tools"][0]["reason"] == "Check gateway summary first."
    assert "Following runbook 'Gateway alert', step 1/3" in context["brief"]


async def test_runbook_step_progression_skips_already_attempted_tools(db_session, monkeypatch):
    _configure_runbook(monkeypatch, _GATEWAY_RUNBOOK_CONFIG)
    task_id = await _gateway_alert_task(db_session)
    db_session.add(
        ToolInvocation(
            task_id=task_id,
            actor_kind="service",
            actor_id="claude",
            tool_id="opnsense.summary",
            provider_id="opnsense",
            status="success",
            started_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    context = await compile_task_context(db_session, task_id)

    assert [tool["tool_id"] for tool in context["recommended_tools"]] == [
        "opnsense.gateways.status",
        "opnsense.interfaces.statistics",
    ]
    assert "step 2/3" in context["brief"]


async def test_runbook_exhausted_surfaces_escalation_note(db_session, monkeypatch):
    _configure_runbook(monkeypatch, _GATEWAY_RUNBOOK_CONFIG)
    task_id = await _gateway_alert_task(db_session)
    for tool_id in ("opnsense.summary", "opnsense.gateways.status", "opnsense.interfaces.statistics"):
        db_session.add(
            ToolInvocation(
                task_id=task_id,
                actor_kind="service",
                actor_id="claude",
                tool_id=tool_id,
                provider_id="opnsense",
                status="success",
                started_at=datetime.now(UTC),
            )
        )
    await db_session.flush()

    context = await compile_task_context(db_session, task_id)

    assert context["recommended_tools"] == []
    assert "exhausted" in context["brief"]
    assert "Ask the operator before further action." in context["brief"]


async def test_scenario_correlated_gateway_alert_matches_runbook(db_session, monkeypatch):
    _configure_runbook(monkeypatch, _GATEWAY_RUNBOOK_CONFIG)
    scenario = Scenario(
        nodes=[
            {"id": "opnsense", "depends_on": []},
            {"id": "homeassistant", "depends_on": ["opnsense"]},
        ],
        steps=[
            Step(
                findings=[
                    {"provider_id": "opnsense", "severity": "critical", "message": "Gateway unreachable"},
                    {"provider_id": "homeassistant", "severity": "warning", "message": "HA unreachable"},
                ]
            ),
        ],
    )
    await run_scenario(db_session, monkeypatch, scenario)

    task = (await db_session.execute(select(Task))).scalar_one()
    context = await compile_task_context(db_session, task.id)

    assert context["recommended_tools"]
    assert context["recommended_tools"][0]["tool_id"] == "opnsense.summary"
    assert "Following runbook 'Gateway alert'" in context["brief"]
