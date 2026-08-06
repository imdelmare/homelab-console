from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Finding,
    Incident,
    ProviderConfiguration,
    Task,
    TaskCheck,
    TaskEvent,
    ToolInvocation,
    WatcherRun,
)
from app.services import dependency_graph, runbooks
from app.services.redaction import redact
from app.services.tasks_service import (
    TaskServiceError,
    check_public,
    finding_public,
    recommended_next_step,
    task_public,
)
from app.tools.registry import ToolDefinition, list_tools

MAX_FINDINGS = 8
MAX_EVENTS = 12
MAX_INVOCATIONS = 10
MAX_WATCHER_RUNS = 5
DEFAULT_BUDGET = {"max_tool_calls": 4, "max_minutes": 10}

SUMMARY_TOOLS_BY_PROVIDER = {
    "adguard": "adguard.summary",
    "asterisk": "asterisk.core.status",
    "cloudflaretunnel": "cloudflare.summary",
    "emqx": "emqx.summary",
    "frigate": "frigate.summary",
    "fritzbox_primary": "fritzbox.primary.summary",
    "fritzbox_secondary": "fritzbox.secondary.summary",
    "homeassistant": "homeassistant.summary",
    "mikrotik": "mikrotik.summary",
    "nextcloud": "nextcloud.summary",
    "nutups": "nutups.summary",
    "opnsense": "opnsense.summary",
    "pbs": "pbs.summary",
    "proxmox": "proxmox.summary",
    "uptimekuma": "uptimekuma.summary",
    "vps": "vps.summary",
}

PROVIDER_TOOL_ALLOWLIST = {
    "adguard": ["adguard.summary", "adguard.status", "adguard.stats", "lab.dns.summary"],
    "asterisk": ["asterisk.core.status", "asterisk.channels.list", "asterisk.peers.list"],
    "cloudflaretunnel": [
        "cloudflare.summary",
        "cloudflare.tunnels.status",
        "cloudflare.connectors.list",
        "vps.summary",
    ],
    "emqx": ["emqx.summary", "emqx.nodes.list", "emqx.stats"],
    "frigate": ["frigate.summary", "frigate.stats", "frigate.cameras.list", "frigate.review.recent"],
    "fritzbox_primary": [
        "fritzbox.primary.summary",
        "fritzbox.primary.wan",
        "fritzbox.primary.wifi",
    ],
    "fritzbox_secondary": [
        "fritzbox.secondary.summary",
        "fritzbox.secondary.wan",
        "fritzbox.secondary.wifi",
    ],
    "glances": [],
    "homeassistant": [
        "homeassistant.summary",
        "homeassistant.states.summary",
        "homeassistant.error_log.tail",
        "homeassistant.logbook.recent",
    ],
    "mikrotik": [
        "mikrotik.summary",
        "mikrotik.system.resource",
        "mikrotik.interfaces.list",
        "mikrotik.lte.status",
    ],
    "nextcloud": ["nextcloud.summary", "nextcloud.status", "nextcloud.serverinfo"],
    "nutups": ["nutups.summary", "nutups.status", "nutups.devices.list"],
    "opnsense": [
        "opnsense.summary",
        "opnsense.gateways.status",
        "opnsense.wireguard.status",
        "opnsense.interfaces.statistics",
        "opnsense.devices.arp",
        "opnsense.kea.leases",
    ],
    "pbs": ["pbs.summary", "pbs.datastores.status", "pbs.tasks.recent", "pbs.verify.jobs.health"],
    "proxmox": [
        "proxmox.summary",
        "proxmox.resources.list",
        "proxmox.tasks.failed",
        "proxmox.backups.list",
    ],
    "uptimekuma": ["uptimekuma.summary", "uptimekuma.monitors.status"],
    "vps": ["vps.summary", "vps.wireguard.status", "vps.deploy.status", "vps.glances.status"],
}

KEYWORD_TOOL_ALLOWLIST = {
    "dns": ["lab.dns.summary", "network.dns.adguard.health"],
    "gateway": ["opnsense.gateways.status", "opnsense.interfaces.statistics"],
    "dhcp": ["opnsense.kea.leases", "opnsense.devices.arp"],
    "kea": ["opnsense.kea.leases", "opnsense.devices.arp"],
    "lease": ["opnsense.kea.leases", "opnsense.devices.arp"],
    "nextcloud": ["nextcloud.summary", "cloudflare.tunnels.status"],
    "power": ["nutups.summary", "nutups.status"],
    "battery": ["nutups.summary", "nutups.status"],
    "ups": ["nutups.summary", "nutups.status"],
    "tunnel": ["cloudflare.summary", "cloudflare.tunnels.status", "vps.wireguard.status"],
    "wireguard": ["opnsense.wireguard.status", "vps.wireguard.status"],
}


@dataclass(frozen=True)
class CompiledIncident:
    incident: Incident | None
    incident_type: str
    provider_ids: list[str]
    root_incident: Incident | None = None
    dependency_path: list[str] = field(default_factory=list)
    matched_runbook: runbooks.Runbook | None = None
    current_step_index: int = 0


async def compile_task_context(db: AsyncSession, task_id: str) -> dict:
    task = await db.get(Task, task_id)
    if task is None:
        raise TaskServiceError("unknown_task", "unknown task")

    incident = await _latest_incident(db, task_id)
    findings = await _findings(db, task_id)
    checks = await _checks(db, task_id)
    events = await _events(db, task_id)
    invocations = await _invocations(db, task_id)
    compiled = await _compile_incident(db, task, incident, findings, invocations, events)
    provider_states = await _provider_states(db, compiled.provider_ids)
    watcher_runs = await _watcher_runs(db, incident.watcher_id if incident else None)
    tools = _recommended_tools(compiled, task, findings)

    open_findings = [finding for finding in findings if finding.resolved_at is None]
    pending_checks = [check for check in checks if check.status == "pending"]
    return {
        "task": task_public(task),
        "incident": _incident_public(incident, compiled.incident_type),
        "provider_ids": compiled.provider_ids,
        "provider_states": provider_states,
        "brief": _brief(
            task,
            incident,
            compiled.incident_type,
            compiled.provider_ids,
            compiled.root_incident,
            compiled.dependency_path,
            compiled.matched_runbook,
            compiled.current_step_index,
        ),
        "recent_findings": [finding_public(finding) for finding in open_findings[:MAX_FINDINGS]],
        "pending_checks": [check_public(check) for check in pending_checks],
        "completed_checks": [
            check_public(check) for check in checks if check.status in {"completed", "skipped"}
        ],
        "recent_tool_invocations": [_invocation_public(invocation) for invocation in invocations],
        "recent_events": [_event_public(event) for event in events],
        "recent_watcher_runs": [_watcher_run_public(run) for run in watcher_runs],
        "recommended_tools": tools,
        "budget": DEFAULT_BUDGET,
        "stop_conditions": [
            "Use read-only tools only.",
            "Pass task_id on infrastructure tool calls so evidence attaches to the task.",
            "Do not perform remediation; ask the operator before any write or risky action.",
            "Update findings/checks/summary before completing or handing off.",
        ],
        "recommended_next_step": recommended_next_step(
            task.status,
            has_pending_checks=bool(pending_checks),
            has_open_critical_findings=any(finding.severity == "critical" for finding in open_findings),
        ),
    }


async def _latest_incident(db: AsyncSession, task_id: str) -> Incident | None:
    return (
        await db.execute(
            select(Incident)
            .where(Incident.task_id == task_id)
            .order_by(Incident.last_seen_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _findings(db: AsyncSession, task_id: str) -> list[Finding]:
    return list(
        (
            await db.execute(
                select(Finding)
                .where(Finding.task_id == task_id)
                .order_by(Finding.resolved_at.is_not(None), Finding.created_at.desc())
                .limit(MAX_FINDINGS)
            )
        ).scalars()
    )


async def _checks(db: AsyncSession, task_id: str) -> list[TaskCheck]:
    return list(
        (
            await db.execute(
                select(TaskCheck).where(TaskCheck.task_id == task_id).order_by(TaskCheck.created_at)
            )
        ).scalars()
    )


async def _events(db: AsyncSession, task_id: str) -> list[TaskEvent]:
    return list(
        (
            await db.execute(
                select(TaskEvent)
                .where(TaskEvent.task_id == task_id)
                .order_by(TaskEvent.created_at.desc())
                .limit(MAX_EVENTS)
            )
        ).scalars()
    )


async def _invocations(db: AsyncSession, task_id: str) -> list[ToolInvocation]:
    return list(
        (
            await db.execute(
                select(ToolInvocation)
                .where(ToolInvocation.task_id == task_id)
                .order_by(ToolInvocation.started_at.desc())
                .limit(MAX_INVOCATIONS)
            )
        ).scalars()
    )


async def _watcher_runs(db: AsyncSession, watcher_id: str | None) -> list[WatcherRun]:
    if not watcher_id:
        return []
    return list(
        (
            await db.execute(
                select(WatcherRun)
                .where(WatcherRun.watcher_id == watcher_id)
                .order_by(WatcherRun.started_at.desc())
                .limit(MAX_WATCHER_RUNS)
            )
        ).scalars()
    )


async def _provider_states(db: AsyncSession, provider_ids: list[str]) -> list[dict]:
    states = []
    for provider_id in provider_ids:
        row = await db.get(ProviderConfiguration, provider_id)
        if row is None:
            states.append({"provider_id": provider_id, "status": "unknown", "last_ok_at": None})
            continue
        states.append(
            {
                "provider_id": row.id,
                "display_name": row.display_name,
                "enabled": row.enabled,
                "status": row.last_status,
                "last_ok_at": row.last_ok_at,
                "updated_at": row.updated_at,
            }
        )
    return states


async def _compile_incident(
    db: AsyncSession,
    task: Task,
    incident: Incident | None,
    findings: list[Finding],
    invocations: list[ToolInvocation],
    events: list[TaskEvent],
) -> CompiledIncident:
    text = _context_text(task, incident, findings)
    provider_context = _latest_provider_context(events)
    provider_ids = []
    if provider_context and isinstance(provider_context.get("provider_id"), str):
        provider_ids.append(provider_context["provider_id"])
    if incident and incident.provider_id:
        provider_ids.append(incident.provider_id)
    provider_ids.extend(_providers_from_text(text))

    root_incident: Incident | None = None
    dependency_path: list[str] = []
    if incident is not None:
        root_incident, dependents = await _root_and_dependents(db, incident)
        if root_incident is not None:
            provider_ids.append(root_incident.provider_id)
            provider_ids.extend(dependent.provider_id for dependent in dependents)
            is_root = incident.id == root_incident.id
            target = dependents[0].provider_id if is_root and dependents else incident.provider_id
            dependency_path = dependency_graph.path_from_root(root_incident.provider_id, target) or []

    incident_type = _incident_type_from_provider_context(provider_context) or _incident_type(incident, text)
    matched_runbook = runbooks.get_runbook(incident_type)
    current_step_index = 0
    if matched_runbook is not None:
        attempted = {invocation.tool_id for invocation in invocations}
        current_step_index = len(matched_runbook.steps)
        for index, step in enumerate(matched_runbook.steps):
            if step.tool_id not in attempted:
                current_step_index = index
                break

    return CompiledIncident(
        incident=incident,
        incident_type=incident_type,
        provider_ids=_dedupe(provider_ids),
        root_incident=root_incident,
        dependency_path=dependency_path,
        matched_runbook=matched_runbook,
        current_step_index=current_step_index,
    )


async def _root_and_dependents(
    db: AsyncSession, incident: Incident
) -> tuple[Incident | None, list[Incident]]:
    """Resolve the correlated cluster for `incident`, in either direction:
    it may itself be a root with dependents (root_cause_incident_id unset),
    or a dependent whose root lives elsewhere. Returns (None, []) — today's
    exact behavior — for any incident with no correlation at all."""
    is_root = incident.root_cause_incident_id is None
    root = incident if is_root else await db.get(Incident, incident.root_cause_incident_id)
    if root is None:
        return None, []
    dependents = list(
        (
            await db.execute(
                select(Incident)
                .where(Incident.root_cause_incident_id == root.id, Incident.id != incident.id)
                .order_by(Incident.last_seen_at.desc())
            )
        ).scalars()
    )
    if is_root and not dependents:
        return None, []
    return root, dependents


def _incident_type(incident: Incident | None, text: str) -> str:
    power_keywords = ("ups", "battery", "power", "on_battery", "low_battery")
    if incident and incident.watcher_id == "lab.alerts":
        if "monitor" in text and "down" in text:
            return "monitor_down"
        if "gateway" in text:
            return "gateway_alert"
        if "wireguard" in text or "tunnel" in text:
            return "connectivity_alert"
        if "backup" in text or "datastore" in text:
            return "backup_alert"
        if _contains_keyword(text, power_keywords):
            return "power_alert"
    if _contains_keyword(text, power_keywords):
        return "power_alert"
    if "dns" in text or "adguard" in text:
        return "dns_alert"
    if "unavailable" in text or "unreachable" in text or "down" in text:
        return "availability_alert"
    return "task_handoff"


def _contains_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    padded = f" {text.replace('-', ' ').replace('_', ' ')} "
    return any(f" {keyword.replace('_', ' ')} " in padded for keyword in keywords)


def _latest_provider_context(events: list[TaskEvent]) -> dict[str, Any] | None:
    for event in events:
        if event.kind == "task.provider_context" and isinstance(event.payload, dict):
            return event.payload
    return None


def _incident_type_from_provider_context(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    hint = str(payload.get("runbook_hint") or "").strip()
    if not hint or hint == "task context fallback":
        status = str(payload.get("status") or "")
        return "availability_alert" if status in {"degraded", "unreachable", "unavailable"} else None
    first = hint.split("/", 1)[0].strip()
    return first or None


def _providers_from_text(text: str) -> list[str]:
    providers = []
    for provider_id in SUMMARY_TOOLS_BY_PROVIDER:
        if provider_id in text:
            providers.append(provider_id)
    if "cloudflare" in text:
        providers.append("cloudflaretunnel")
    if "dns" in text:
        providers.append("adguard")
    if "gateway" in text or "wireguard" in text:
        providers.append("opnsense")
    if _contains_keyword(text, ("ups", "battery", "power")):
        providers.append("nutups")
    return providers


def _context_text(task: Task, incident: Incident | None, findings: list[Finding]) -> str:
    parts: list[str] = [task.title, task.goal, task.summary or ""]
    if incident is not None:
        parts.extend([incident.provider_id, incident.title, incident.description])
    for finding in findings:
        parts.extend([finding.title, finding.description, finding.source])
    return " ".join(parts).lower()


def _recommended_tools(compiled: CompiledIncident, task: Task, findings: list[Finding]) -> list[dict]:
    available = {tool.id: tool for tool in list_tools() if tool.enabled and tool.mode == "read"}

    if compiled.matched_runbook is not None:
        remaining = compiled.matched_runbook.steps[compiled.current_step_index :]
        return [
            _tool_recommendation(available[step.tool_id], step.evidence)
            for step in remaining
            if step.tool_id in available
        ][: DEFAULT_BUDGET["max_tool_calls"]]

    text = _context_text(task, compiled.incident, findings)
    tool_ids: list[str] = []
    for provider_id in compiled.provider_ids:
        tool_ids.extend(PROVIDER_TOOL_ALLOWLIST.get(provider_id, []))
        summary_tool = SUMMARY_TOOLS_BY_PROVIDER.get(provider_id)
        if summary_tool:
            tool_ids.insert(0, summary_tool)
    for keyword, ids in KEYWORD_TOOL_ALLOWLIST.items():
        if _contains_keyword(text, (keyword,)):
            tool_ids.extend(ids)
    if not tool_ids:
        tool_ids.extend(["lab.summary", "lab.alerts.recent"])
    return [
        _tool_recommendation(available[tool_id], _tool_reason(tool_id, compiled.incident_type))
        for tool_id in _dedupe(tool_ids)
        if tool_id in available
    ][: DEFAULT_BUDGET["max_tool_calls"]]


def _tool_recommendation(tool: ToolDefinition, reason: str) -> dict:
    return {
        "tool_id": tool.id,
        "name": tool.name,
        "provider_id": tool.provider_id,
        "reason": reason,
        "input_schema": tool.input_model.model_json_schema(),
    }


def _tool_reason(tool_id: str, incident_type: str) -> str:
    if tool_id.endswith(".summary"):
        return f"Start with the compact provider summary for {incident_type} evidence."
    if "wireguard" in tool_id or "tunnel" in tool_id:
        return "Check the connectivity path before blaming the application."
    if "gateway" in tool_id or "interfaces" in tool_id:
        return "Check network edge state and interface counters."
    if "alerts.recent" in tool_id:
        return "Refresh the deterministic alert source."
    return "Collect bounded read-only evidence relevant to the task."


def _incident_public(incident: Incident | None, incident_type: str) -> dict | None:
    if incident is None:
        return {"type": incident_type}
    return {
        "id": incident.id,
        "type": incident_type,
        "dedupe_key": incident.dedupe_key,
        "watcher_id": incident.watcher_id,
        "status": incident.status,
        "severity": incident.severity,
        "provider_id": incident.provider_id,
        "title": incident.title,
        "description": incident.description,
        "first_seen_at": incident.first_seen_at,
        "last_seen_at": incident.last_seen_at,
        "occurrences": incident.occurrences,
        "missing_runs": incident.missing_runs,
        "payload": redact(incident.payload),
    }


def _invocation_public(invocation: ToolInvocation) -> dict:
    return {
        "id": invocation.id,
        "tool_id": invocation.tool_id,
        "status": invocation.status,
        "error_code": invocation.error_code,
        "started_at": invocation.started_at,
        "duration_ms": invocation.duration_ms,
    }


def _event_public(event: TaskEvent) -> dict:
    return {"id": event.id, "kind": event.kind, "payload": event.payload, "created_at": event.created_at}


def _watcher_run_public(run: WatcherRun) -> dict:
    return {
        "id": run.id,
        "watcher_id": run.watcher_id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "created_tasks": run.created_tasks,
        "updated_incidents": run.updated_incidents,
        "resolved_incidents": run.resolved_incidents,
        "error": run.error,
        "payload": redact(run.payload),
    }


def _brief(
    task: Task,
    incident: Incident | None,
    incident_type: str,
    provider_ids: list[str],
    root_incident: Incident | None = None,
    dependency_path: list[str] | None = None,
    matched_runbook: runbooks.Runbook | None = None,
    current_step_index: int = 0,
) -> str:
    provider_text = ", ".join(provider_ids) if provider_ids else "no provider identified"
    if incident is None:
        if matched_runbook is not None:
            total = len(matched_runbook.steps)
            if current_step_index < total:
                step = matched_runbook.steps[current_step_index]
                return (
                    f"{incident_type} on {provider_text}. Following runbook '{matched_runbook.label}', "
                    f"step {current_step_index + 1}/{total}: {step.evidence}"
                )[:800]
            return (
                f"{incident_type} on {provider_text}. Runbook '{matched_runbook.label}' exhausted "
                f"({total}/{total} steps tried without resolving it). {matched_runbook.escalation_note}"
            )[:800]
        return f"Task '{task.title}' has no linked incident. Use task evidence and bounded read-only tools."
    if matched_runbook is not None:
        total = len(matched_runbook.steps)
        if current_step_index < total:
            step = matched_runbook.steps[current_step_index]
            return (
                f"{incident_type} on {provider_text}. Following runbook '{matched_runbook.label}', "
                f"step {current_step_index + 1}/{total}: {step.evidence}"
            )[:800]
        return (
            f"{incident_type} on {provider_text}. Runbook '{matched_runbook.label}' exhausted "
            f"({total}/{total} steps tried without resolving it). {matched_runbook.escalation_note}"
        )[:800]
    if root_incident is not None and dependency_path:
        chain = " → ".join(dependency_graph.label_of(node_id) for node_id in dependency_path)
        return (
            f"{incident_type} on {provider_text}. Dependency graph indicates a shared root cause: {chain}. "
            f"Root incident: {root_incident.title}. Confirm the root cause first with its recommended tools "
            "before treating the other affected providers as separate problems."
        )[:800]
    return (
        f"{incident_type} on {provider_text}: {incident.title}. "
        "Verify the current state with the recommended read-only tools, separate detection from root cause, "
        "and update the task with concrete evidence."
    )[:800]


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
