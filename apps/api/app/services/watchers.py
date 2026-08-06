from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.locks import try_advisory_xact_lock
from app.db.models import (
    Incident,
    Task,
    TaskCheck,
    TaskEvent,
    WatcherAutomationState,
    WatcherConfig,
    WatcherRun,
    utcnow,
)
from app.db.session import get_session_factory
from app.domain.actors import Actor
from app.services import dependency_graph
from app.services import inventory
from app.services.incident_matcher import IncidentMatchDecision, match_incident
from app.services.notification_outbox import cancel_pending_for_incident, enqueue_watcher_incident
from app.services.luna_metrics import record_llm_usage
from app.services.task_notifications import _task_keyboard
from app.services.audit import write_audit
from app.services.auto_investigation import maybe_auto_investigate
from app.services.redaction import redact
from app.services.task_router_queue import enqueue_task_routing
from app.services.tasks_service import (
    FINAL_TASK_STATUSES,
    TRANSITION_POLICY_OPERATOR_HANDLED,
    TRANSITION_POLICY_WATCHER_AUTO_CLEARED,
    TaskServiceError,
    add_finding,
    create_task,
    transition_task,
)
from app.tools.execution import execute_tool

logger = logging.getLogger("homelab.watchers")

WATCHER_ACTOR = Actor(kind="service", id="watcher", label="Watcher")
IMMEDIATE_CRITICAL_WATCHERS = {
    "power.ups",
    "security.certificates",
    "storage.disks",
    "thermal.sensors",
}
CONNECTIVITY_WATCHERS = {
    "cloudflare.tunnel",
    "lab.alerts",
    "network.gateway",
    "network.wireguard",
    "network.zerotier",
    "uptimekuma.monitors",
}
WATCHER_IDS = {
    "backup.freshness",
    "cloudflare.tunnel",
    "lab.alerts",
    "network.gateway",
    "network.presence",
    "network.zerotier",
    "network.wireguard",
    "power.ups",
    "security.certificates",
    "storage.disks",
    "thermal.sensors",
    "uptimekuma.monitors",
}
DEFAULT_WATCHER_IDS = {
    "cloudflare.tunnel",
    "lab.alerts",
    "network.gateway",
    "network.zerotier",
    "network.wireguard",
    "power.ups",
    "uptimekuma.monitors",
}
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
_watcher_overrides: dict[str, dict[str, Any]] = {}

WATCHER_LABELS = {
    "backup.freshness": "Backup freshness",
    "cloudflare.tunnel": "Cloudflare Tunnel",
    "lab.alerts": "Lab alerts",
    "network.gateway": "Gateway/WAN",
    "network.presence": "Network presence",
    "network.zerotier": "ZeroTier",
    "network.wireguard": "WireGuard",
    "power.ups": "UPS power",
    "security.certificates": "TLS certificates",
    "storage.disks": "Disk health",
    "thermal.sensors": "Thermal sensors",
    "uptimekuma.monitors": "Uptime Kuma",
}

WATCHER_RUNBOOKS = {
    "backup.freshness": None,
    "cloudflare.tunnel": "connectivity_alert",
    "lab.alerts": None,
    "network.gateway": "gateway_alert",
    "network.presence": None,
    "network.zerotier": "connectivity_alert",
    "network.wireguard": "connectivity_alert",
    "power.ups": "power_alert",
    "security.certificates": None,
    "storage.disks": None,
    "thermal.sensors": None,
    "uptimekuma.monitors": None,
}

BACKUP_DEFAULT_MAX_AGE_DAYS = 3.0
DISK_HEALTH_OK_VALUES = {"", "passed", "ok", "unknown"}

THERMAL_TOOL_IDS = [
    "nutups.status",
    "opnsense.system.temperature",
    "mikrotik.system.health",
    "fritzbox.primary.temperature",
    "fritzbox.secondary.temperature",
    "proxmox.disks.temperatures",
    "hosts.temperatures",
    "frigate.stats",
]
THERMAL_THRESHOLDS = {
    "ups": {"warning": 38.0, "critical": 45.0},
    "opnsense": {"warning": 75.0, "critical": 85.0},
    "mikrotik": {"warning": 70.0, "critical": 80.0},
    "fritzbox": {"warning": 80.0, "critical": 90.0},
    "disk": {"warning": 55.0, "critical": 65.0},
    "edge": {"warning": 70.0, "critical": 80.0},
    "compute": {"warning": 80.0, "critical": 90.0},
    "frigate": {"warning": 75.0, "critical": 85.0},
}


@dataclass(frozen=True)
class DetectedIncident:
    watcher_id: str
    dedupe_key: str
    dedupe_basis: str
    severity: str
    provider_id: str
    title: str
    description: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class UpsertResult:
    created: bool  # True only when a brand-new Task was created
    incident_id: str
    effective_root_id: str | None  # this incident's own id if it IS a root, else the ultimate ancestor's id


@dataclass(frozen=True)
class WatcherRuntimeConfig:
    watcher_id: str
    enabled: bool
    interval_seconds: int
    min_severity: str
    investigation_mode: str


def incident_public(incident: Incident) -> dict[str, Any]:
    return {
        "id": incident.id,
        "dedupe_key": incident.dedupe_key,
        "watcher_id": incident.watcher_id,
        "status": incident.status,
        "severity": incident.severity,
        "provider_id": incident.provider_id,
        "title": incident.title,
        "description": incident.description,
        "task_id": incident.task_id,
        "first_seen_at": incident.first_seen_at,
        "last_seen_at": incident.last_seen_at,
        "resolved_at": incident.resolved_at,
        "resolution_reason": incident.resolution_reason,
        "missing_runs": incident.missing_runs,
        "last_missing_at": incident.last_missing_at,
        "occurrences": incident.occurrences,
        "payload": incident.payload,
        "root_cause_incident_id": incident.root_cause_incident_id,
        "dedupe_basis": incident.payload.get("dedupe_basis") if isinstance(incident.payload, dict) else "",
        "dedupe_note": _incident_dedupe_note(incident),
        "auto_close_note": _incident_auto_close_note(incident),
        "runbook_incident_type": _incident_runbook_type(incident),
    }


def watcher_run_public(run: WatcherRun) -> dict[str, Any]:
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
        "payload": run.payload,
    }


async def watcher_status(db: AsyncSession | None = None) -> dict[str, Any]:
    settings = get_settings()
    automation = settings.watchers_enabled
    if db is not None:
        await _load_watcher_configs(db)
        automation = await _load_watcher_automation_enabled(db)
    watcher_items = await _watcher_status_items(db, automation=automation) if db is not None else []
    return {
        "enabled": automation,
        "interval_seconds": max(60, settings.watchers_interval_seconds),
        "watcher_ids": sorted(WATCHER_IDS),
        "scheduled_watcher_ids": sorted(_enabled_watcher_ids()),
        "min_severity": _minimum_severity(),
        "ignore_patterns": settings.watchers_ignore_pattern_list,
        "resolve_after_missing_runs": _resolve_after_missing_runs(),
        "watchers": watcher_items,
    }


def reset_runtime_state_for_tests() -> None:
    _watcher_overrides.clear()


async def set_watcher_automation_enabled(
    enabled: bool,
    db: AsyncSession | None = None,
    *,
    actor: Actor | None = None,
) -> dict[str, Any]:
    if db is None:
        raise TaskServiceError("invalid_input", "database session is required")
    updated_by = actor.audit_id() if actor else ""
    updated = await db.execute(
        update(WatcherAutomationState)
        .where(WatcherAutomationState.id == "global")
        .values(
            enabled=enabled,
            revision=WatcherAutomationState.revision + 1,
            updated_by=updated_by,
            updated_at=utcnow(),
        )
        .returning(WatcherAutomationState.revision)
    )
    revision = updated.scalar_one_or_none()
    if revision is None:
        try:
            async with db.begin_nested():
                row = WatcherAutomationState(
                    id="global",
                    enabled=enabled,
                    revision=1,
                    updated_by=updated_by,
                )
                db.add(row)
                await db.flush()
            revision = 1
        except IntegrityError:
            # Another worker created the singleton between our UPDATE and
            # INSERT. Apply this request to that row instead.
            updated = await db.execute(
                update(WatcherAutomationState)
                .where(WatcherAutomationState.id == "global")
                .values(
                    enabled=enabled,
                    revision=WatcherAutomationState.revision + 1,
                    updated_by=updated_by,
                    updated_at=utcnow(),
                )
                .returning(WatcherAutomationState.revision)
            )
            revision = updated.scalar_one()
    await write_audit(
        db,
        actor=actor or WATCHER_ACTOR,
        source="rest" if actor else "system",
        action="watcher.automation.updated",
        outcome="success",
        metadata={"enabled": enabled, "revision": revision},
    )
    return await watcher_status(db)


async def configure_watcher(
    db: AsyncSession | None,
    watcher_id: str,
    *,
    enabled: bool | None = None,
    interval_seconds: int | None = None,
    min_severity: str | None = None,
    investigation_mode: str | None = None,
) -> dict[str, Any]:
    if watcher_id not in WATCHER_IDS:
        raise TaskServiceError("unknown_watcher", "unknown watcher")
    patch = _watcher_overrides.setdefault(watcher_id, {})
    if enabled is not None:
        patch["enabled"] = enabled
    if interval_seconds is not None:
        patch["interval_seconds"] = max(60, interval_seconds)
    if min_severity is not None:
        configured = min_severity.lower()
        if configured not in {"warning", "critical"}:
            raise TaskServiceError("invalid_input", "min_severity must be warning or critical")
        patch["min_severity"] = configured
    if investigation_mode is not None:
        configured_mode = investigation_mode.lower()
        if configured_mode not in {"manual", "auto_investigate"}:
            raise TaskServiceError(
                "invalid_input",
                "investigation_mode must be manual or auto_investigate",
            )
        patch["investigation_mode"] = configured_mode
    if db is not None:
        row = await db.get(WatcherConfig, watcher_id)
        config = _watcher_config(watcher_id)
        if row is None:
            row = WatcherConfig(watcher_id=watcher_id)
            db.add(row)
        row.enabled = config.enabled
        row.interval_seconds = config.interval_seconds
        row.min_severity = config.min_severity
        row.investigation_mode = config.investigation_mode
        row.updated_at = utcnow()
        await db.flush()
    return await watcher_status(db)


async def list_incidents(
    db: AsyncSession,
    *,
    status: str | None = "open",
    limit: int = 100,
) -> list[Incident]:
    limit = max(1, min(limit, 100))
    query = select(Incident)
    if status:
        query = query.where(Incident.status == status)
    result = await db.execute(query.order_by(Incident.last_seen_at.desc()).limit(limit))
    return list(result.scalars())


async def list_watcher_runs(db: AsyncSession, *, limit: int = 50) -> list[WatcherRun]:
    limit = max(1, min(limit, 100))
    result = await db.execute(select(WatcherRun).order_by(WatcherRun.started_at.desc()).limit(limit))
    return list(result.scalars())


async def resolve_incident_as_handled(
    db: AsyncSession,
    *,
    incident_id: str,
    actor: Actor,
    note: str = "",
) -> Incident:
    incident = await db.get(Incident, incident_id)
    if incident is None:
        raise TaskServiceError("unknown_incident", "unknown incident")

    now = utcnow()
    if incident.status != "resolved":
        incident.status = "resolved"
        incident.resolved_at = now
        incident.resolution_reason = "operator_already_handled"
        incident.missing_runs = 0
        incident.last_missing_at = None
    await cancel_pending_for_incident(
        db, provider_id=incident.provider_id, dedupe_key=incident.dedupe_key
    )

    task = await db.get(Task, incident.task_id) if incident.task_id else None
    if task is not None:
        await _close_task_for_handled_incident(
            db,
            task=task,
            incident=incident,
            actor=actor,
            note=note,
            now=now,
        )

    await write_audit(
        db,
        actor=actor,
        source="rest",
        action="watcher.incident.resolve_handled",
        outcome="success",
        task_id=incident.task_id or "",
        metadata={
            "incident_id": incident.id,
            "watcher_id": incident.watcher_id,
            "provider_id": incident.provider_id,
            "note": note[:1000],
        },
    )
    await db.flush()
    return incident


async def run_watchers(
    db: AsyncSession,
    *,
    watcher_ids: set[str] | None = None,
    actor: Actor = WATCHER_ACTOR,
) -> dict[str, Any]:
    await _load_watcher_configs(db)
    selected = watcher_ids if watcher_ids is not None else _enabled_watcher_ids()
    unknown = selected - WATCHER_IDS
    if unknown:
        return {
            "ok": False,
            "error": f"unknown watcher(s): {', '.join(sorted(unknown))}",
            "watchers": [],
            "created_tasks": 0,
            "updated_incidents": 0,
            "resolved_incidents": 0,
        }

    watcher_results = []
    created_tasks = 0
    updated_incidents = 0
    resolved_incidents = 0
    for watcher_id in sorted(selected):
        if watcher_ids is None and not _watcher_config(watcher_id).enabled:
            continue
        if watcher_id == "backup.freshness":
            result = await _run_backup_freshness_watcher(db, actor=actor)
            watcher_results.append(result)
            created_tasks += int(result.get("created_tasks", 0))
            updated_incidents += int(result.get("updated_incidents", 0))
            resolved_incidents += int(result.get("resolved_incidents", 0))
        elif watcher_id == "cloudflare.tunnel":
            result = await _run_simple_network_watcher(
                db,
                actor=actor,
                watcher_id="cloudflare.tunnel",
                tool_id="cloudflare.summary",
                detector=_cloudflare_tunnel_incidents,
            )
            watcher_results.append(result)
            created_tasks += int(result.get("created_tasks", 0))
            updated_incidents += int(result.get("updated_incidents", 0))
            resolved_incidents += int(result.get("resolved_incidents", 0))
        elif watcher_id == "lab.alerts":
            result = await _run_lab_alerts_watcher(db, actor=actor)
            watcher_results.append(result)
            created_tasks += int(result.get("created_tasks", 0))
            updated_incidents += int(result.get("updated_incidents", 0))
            resolved_incidents += int(result.get("resolved_incidents", 0))
        elif watcher_id == "network.presence":
            result = await _run_network_presence_watcher(db, actor=actor)
            watcher_results.append(result)
            created_tasks += int(result.get("created_tasks", 0))
            updated_incidents += int(result.get("updated_incidents", 0))
            resolved_incidents += int(result.get("resolved_incidents", 0))
        elif watcher_id == "network.gateway":
            result = await _run_simple_network_watcher(
                db,
                actor=actor,
                watcher_id="network.gateway",
                tool_id="opnsense.gateways.status",
                detector=_network_gateway_incidents,
            )
            watcher_results.append(result)
            created_tasks += int(result.get("created_tasks", 0))
            updated_incidents += int(result.get("updated_incidents", 0))
            resolved_incidents += int(result.get("resolved_incidents", 0))
        elif watcher_id == "network.zerotier":
            result = await _run_simple_network_watcher(
                db,
                actor=actor,
                watcher_id="network.zerotier",
                tool_id="zerotier.members.list",
                detector=_network_zerotier_incidents,
            )
            watcher_results.append(result)
            created_tasks += int(result.get("created_tasks", 0))
            updated_incidents += int(result.get("updated_incidents", 0))
            resolved_incidents += int(result.get("resolved_incidents", 0))
        elif watcher_id == "network.wireguard":
            result = await _run_simple_network_watcher(
                db,
                actor=actor,
                watcher_id="network.wireguard",
                tool_id="opnsense.wireguard.status",
                detector=_network_wireguard_incidents,
            )
            watcher_results.append(result)
            created_tasks += int(result.get("created_tasks", 0))
            updated_incidents += int(result.get("updated_incidents", 0))
            resolved_incidents += int(result.get("resolved_incidents", 0))
        elif watcher_id == "power.ups":
            result = await _run_simple_network_watcher(
                db,
                actor=actor,
                watcher_id="power.ups",
                tool_id="nutups.status",
                detector=_power_ups_incidents,
            )
            watcher_results.append(result)
            created_tasks += int(result.get("created_tasks", 0))
            updated_incidents += int(result.get("updated_incidents", 0))
            resolved_incidents += int(result.get("resolved_incidents", 0))
        elif watcher_id == "security.certificates":
            result = await _run_simple_network_watcher(
                db,
                actor=actor,
                watcher_id="security.certificates",
                tool_id="network.tls.certificates",
                detector=_certificate_incidents,
            )
            watcher_results.append(result)
            created_tasks += int(result.get("created_tasks", 0))
            updated_incidents += int(result.get("updated_incidents", 0))
            resolved_incidents += int(result.get("resolved_incidents", 0))
        elif watcher_id == "storage.disks":
            result = await _run_disk_health_watcher(db, actor=actor)
            watcher_results.append(result)
            created_tasks += int(result.get("created_tasks", 0))
            updated_incidents += int(result.get("updated_incidents", 0))
            resolved_incidents += int(result.get("resolved_incidents", 0))
        elif watcher_id == "thermal.sensors":
            result = await _run_thermal_sensors_watcher(db, actor=actor)
            watcher_results.append(result)
            created_tasks += int(result.get("created_tasks", 0))
            updated_incidents += int(result.get("updated_incidents", 0))
            resolved_incidents += int(result.get("resolved_incidents", 0))
        elif watcher_id == "uptimekuma.monitors":
            result = await _run_simple_network_watcher(
                db,
                actor=actor,
                watcher_id="uptimekuma.monitors",
                tool_id="uptimekuma.monitors.status",
                detector=_uptimekuma_monitor_incidents,
            )
            watcher_results.append(result)
            created_tasks += int(result.get("created_tasks", 0))
            updated_incidents += int(result.get("updated_incidents", 0))
            resolved_incidents += int(result.get("resolved_incidents", 0))
    return {
        "ok": all(item.get("status") in {"success", "skipped"} for item in watcher_results),
        "watchers": watcher_results,
        "created_tasks": created_tasks,
        "updated_incidents": updated_incidents,
        "resolved_incidents": resolved_incidents,
    }


async def _close_task_for_handled_incident(
    db: AsyncSession,
    *,
    task: Task,
    incident: Incident,
    actor: Actor,
    note: str,
    now,
) -> None:
    completed_now = task.status not in FINAL_TASK_STATUSES
    if completed_now:
        suffix = (
            f"Watcher incident {incident.id[:8]} marked already handled by {actor.audit_id()}."
            f"{' Note: ' + note[:1000] if note else ''}"
        )
        task.summary = f"{task.summary}\n\n{suffix}".strip()[:8000]

        pending_checks = (
            await db.execute(
                select(TaskCheck).where(TaskCheck.task_id == task.id, TaskCheck.status == "pending")
            )
        ).scalars()
        for check in pending_checks:
            check.status = "skipped"
            check.skip_reason = "Watcher incident marked already handled by operator."
            check.completed_by = actor.audit_id()
            check.completed_at = now

        await transition_task(
            db,
            task,
            "completed",
            actor,
            source="watcher",
            policy=TRANSITION_POLICY_OPERATOR_HANDLED,
            reason="operator_already_handled",
            incident_id=incident.id,
            notify=False,
        )

    db.add(
        TaskEvent(
            task_id=task.id,
            kind="watcher.incident.resolve_handled",
            payload=redact(
                {
                    "incident_id": incident.id,
                    "provider_id": incident.provider_id,
                    "resolution_reason": "operator_already_handled",
                    "note": note[:1000],
                }
            ),
        )
    )


async def _maybe_auto_complete_cleared_task(
    db: AsyncSession,
    *,
    incident: Incident,
    actor: Actor,
    now,
) -> bool:
    if not incident.task_id:
        return False
    task = await db.get(Task, incident.task_id)
    if task is None:
        return False
    if task.source != "watcher":
        return False
    if task.status != "open" or task.assigned_agent or task.claimed_at is not None:
        return False

    linked_open = (
        await db.execute(
            select(Incident.id)
            .where(
                Incident.task_id == task.id,
                Incident.status == "open",
                Incident.id != incident.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if linked_open is not None:
        return False

    pending_checks = (
        await db.execute(
            select(TaskCheck).where(TaskCheck.task_id == task.id, TaskCheck.status == "pending")
        )
    ).scalars()
    for check in pending_checks:
        check.status = "skipped"
        check.skip_reason = "Watcher alert cleared before the task was claimed."
        check.completed_by = actor.audit_id()
        check.completed_at = now

    suffix = (
        f"Auto-resolved by watcher: incident {incident.id[:8]} cleared after "
        f"{incident.missing_runs} missing run(s). No operator action was required."
    )
    task.summary = f"{task.summary}\n\n{suffix}".strip()[:8000]
    await transition_task(
        db,
        task,
        "completed",
        actor,
        source="watcher",
        policy=TRANSITION_POLICY_WATCHER_AUTO_CLEARED,
        reason="alert_cleared",
        incident_id=incident.id,
        notify=False,
    )
    db.add(
        TaskEvent(
            task_id=task.id,
            kind="watcher.task.auto_completed",
            payload=redact(
                {
                    "incident_id": incident.id,
                    "watcher_id": incident.watcher_id,
                    "provider_id": incident.provider_id,
                    "resolution_reason": "alert_cleared",
                }
            ),
        )
    )
    return True


async def _run_lab_alerts_watcher(db: AsyncSession, *, actor: Actor) -> dict[str, Any]:
    run = WatcherRun(watcher_id="lab.alerts", status="running")
    db.add(run)
    await db.flush()
    if not await _try_acquire_watcher_lock(db, "lab.alerts"):
        run.status = "skipped"
        run.finished_at = utcnow()
        run.payload = {"reason": "watcher_already_running"}
        return watcher_run_public(run)

    try:
        tool_result = await execute_tool("lab.alerts.recent", {}, actor, source="watcher")
        if not tool_result.ok:
            message = tool_result.error.message if tool_result.error else "tool failed"
            run.status = "error"
            run.error = message
            run.finished_at = utcnow()
            run.payload = {"tool_id": "lab.alerts.recent", "error": message}
            return watcher_run_public(run)

        summary = (tool_result.result or {}).get("summary", {})
        findings = summary.get("findings", []) if isinstance(summary, dict) else []
        observed = [
            _incident_from_finding(item) for item in findings if _is_observable_finding(item)
        ]
        actionable = [item for item in findings if _is_actionable(item, "lab.alerts")]
        detected = [_incident_from_finding(item) for item in actionable]
        observed_keys = {incident.dedupe_key for incident in observed}
        actionable_keys = {incident.dedupe_key for incident in detected}
        await _refresh_filtered_open_incidents(
            db, observed, actionable_keys=actionable_keys, watcher_id="lab.alerts"
        )
        present_ids = {incident.provider_id for incident in detected}
        root_by_provider = await _open_incident_provider_ids(db, "lab.alerts")
        rootable_ids = {incident.provider_id for incident in detected if incident.severity == "critical"}
        rootable_ids |= await _open_critical_root_provider_ids(db, "lab.alerts")
        correlated_ids = present_ids | set(root_by_provider)
        for incident in _topo_order(detected, present_ids):
            root_id = _pick_root_candidate(
                incident.provider_id,
                correlated_ids,
                root_by_provider,
                rootable_ids=rootable_ids,
            )
            result = await _upsert_incident(db, incident, actor=actor, root_incident_id=root_id)
            root_by_provider[incident.provider_id] = result.effective_root_id or result.incident_id
            if result.created:
                run.created_tasks += 1
            else:
                run.updated_incidents += 1
        resolved, clearing = await _resolve_missing_incidents(
            db,
            watcher_id="lab.alerts",
            observed_keys=observed_keys,
            actor=actor,
        )
        run.resolved_incidents = resolved

        run.status = "success"
        run.finished_at = utcnow()
        run.payload = {
            "tool_id": "lab.alerts.recent",
            "findings_total": len(findings),
            "actionable_total": len(detected),
            "ignored_total": len(findings) - len(actionable),
            "min_severity": _minimum_severity(),
            "resolve_after_missing_runs": _resolve_after_missing_runs(),
            "clearing_total": clearing,
            "resolved_total": run.resolved_incidents,
        }
        return watcher_run_public(run)
    except Exception as exc:
        logger.exception("watcher failed: lab.alerts")
        run.status = "error"
        run.error = exc.__class__.__name__
        run.finished_at = utcnow()
        run.payload = {"tool_id": "lab.alerts.recent"}
        return watcher_run_public(run)


async def _run_network_presence_watcher(db: AsyncSession, *, actor: Actor) -> dict[str, Any]:
    watcher_id = "network.presence"
    run = WatcherRun(watcher_id=watcher_id, status="running")
    db.add(run)
    await db.flush()
    if not await _try_acquire_watcher_lock(db, watcher_id):
        run.status = "skipped"
        run.finished_at = utcnow()
        run.payload = {"reason": "watcher_already_running"}
        return watcher_run_public(run)

    try:
        arp_result = await execute_tool("opnsense.devices.arp", {}, actor, source="watcher")
        leases_result = await execute_tool("opnsense.kea.leases", {}, actor, source="watcher")
        if not arp_result.ok or not leases_result.ok:
            failed = arp_result if not arp_result.ok else leases_result
            message = failed.error.message if failed.error else "tool failed"
            run.status = "error"
            run.error = message
            run.finished_at = utcnow()
            run.payload = {
                "tool_ids": ["opnsense.devices.arp", "opnsense.kea.leases"],
                "error": message,
            }
            return watcher_run_public(run)

        arp_devices = (arp_result.result or {}).get("devices", [])
        leases = (leases_result.result or {}).get("leases", [])
        snapshot = _network_presence_snapshot(arp_devices, leases)
        previous = await _latest_success_payload(db, watcher_id)
        previous_macs = set(previous.get("observed_macs", [])) if previous else set()
        is_baseline = not previous_macs
        observed = [] if is_baseline else _network_presence_incidents(snapshot, previous_macs)
        detected = _filter_detected(watcher_id, observed)
        observed_keys = {incident.dedupe_key for incident in observed}
        actionable_keys = {incident.dedupe_key for incident in detected}
        await _refresh_filtered_open_incidents(
            db, observed, actionable_keys=actionable_keys, watcher_id=watcher_id
        )

        for incident in detected:
            result = await _upsert_incident(db, incident, actor=actor)
            if result.created:
                run.created_tasks += 1
            else:
                run.updated_incidents += 1

        resolved, clearing = await _resolve_missing_incidents(
            db,
            watcher_id=watcher_id,
            observed_keys=observed_keys,
            actor=actor,
        )
        run.resolved_incidents = resolved
        run.status = "success"
        run.finished_at = utcnow()
        run.payload = {
            "tool_ids": ["opnsense.devices.arp", "opnsense.kea.leases"],
            "baseline": is_baseline,
            "observed_macs": sorted(snapshot["observed_macs"]),
            "observed_ips": sorted(snapshot["observed_ips"]),
            "arp_total": len(arp_devices) if isinstance(arp_devices, list) else 0,
            "leases_total": len(leases) if isinstance(leases, list) else 0,
            "new_macs_total": 0 if is_baseline else len(snapshot["observed_macs"] - previous_macs),
            "anomalies_total": len(detected),
            "clearing_total": clearing,
            "resolved_total": run.resolved_incidents,
        }
        return watcher_run_public(run)
    except Exception as exc:
        logger.exception("watcher failed: network.presence")
        run.status = "error"
        run.error = exc.__class__.__name__
        run.finished_at = utcnow()
        run.payload = {"tool_ids": ["opnsense.devices.arp", "opnsense.kea.leases"]}
        return watcher_run_public(run)


async def _run_simple_network_watcher(
    db: AsyncSession,
    *,
    actor: Actor,
    watcher_id: str,
    tool_id: str,
    detector,
) -> dict[str, Any]:
    run = WatcherRun(watcher_id=watcher_id, status="running")
    db.add(run)
    await db.flush()
    if not await _try_acquire_watcher_lock(db, watcher_id):
        run.status = "skipped"
        run.finished_at = utcnow()
        run.payload = {"reason": "watcher_already_running"}
        return watcher_run_public(run)

    try:
        tool_result = await execute_tool(tool_id, {}, actor, source="watcher")
        if not tool_result.ok:
            message = tool_result.error.message if tool_result.error else "tool failed"
            run.status = "error"
            run.error = message
            run.finished_at = utcnow()
            run.payload = {"tool_id": tool_id, "error": message}
            return watcher_run_public(run)

        payload = tool_result.result or {}
        observed = detector(payload)
        detected = _filter_detected(watcher_id, observed)
        observed_keys = {incident.dedupe_key for incident in observed}
        actionable_keys = {incident.dedupe_key for incident in detected}
        await _refresh_filtered_open_incidents(
            db, observed, actionable_keys=actionable_keys, watcher_id=watcher_id
        )
        root_by_provider = await _open_incident_provider_ids(db, watcher_id)
        for incident in detected:
            result = await _upsert_incident(
                db,
                incident,
                actor=actor,
                root_incident_id=root_by_provider.get(incident.provider_id),
            )
            root_by_provider[incident.provider_id] = result.effective_root_id or result.incident_id
            if result.created:
                run.created_tasks += 1
            else:
                run.updated_incidents += 1

        resolved, clearing = await _resolve_missing_incidents(
            db,
            watcher_id=watcher_id,
            observed_keys=observed_keys,
            actor=actor,
        )
        run.resolved_incidents = resolved
        run.status = "success"
        run.finished_at = utcnow()
        run.payload = {
            "tool_id": tool_id,
            "findings_total": len(detected),
            "clearing_total": clearing,
            "resolved_total": run.resolved_incidents,
        }
        return watcher_run_public(run)
    except Exception as exc:
        logger.exception("watcher failed: %s", watcher_id)
        run.status = "error"
        run.error = exc.__class__.__name__
        run.finished_at = utcnow()
        run.payload = {"tool_id": tool_id}
        return watcher_run_public(run)


async def _run_thermal_sensors_watcher(db: AsyncSession, *, actor: Actor) -> dict[str, Any]:
    watcher_id = "thermal.sensors"
    run = WatcherRun(watcher_id=watcher_id, status="running")
    db.add(run)
    await db.flush()
    if not await _try_acquire_watcher_lock(db, watcher_id):
        run.status = "skipped"
        run.finished_at = utcnow()
        run.payload = {"reason": "watcher_already_running"}
        return watcher_run_public(run)

    try:
        tool_payloads: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for tool_id in THERMAL_TOOL_IDS:
            tool_result = await execute_tool(tool_id, {}, actor, source="watcher")
            if tool_result.ok:
                tool_payloads[tool_id] = tool_result.result or {}
            else:
                errors[tool_id] = tool_result.error.message if tool_result.error else "tool failed"

        if not tool_payloads:
            run.status = "error"
            run.error = "; ".join(errors.values())[:500]
            run.finished_at = utcnow()
            run.payload = {"tool_ids": THERMAL_TOOL_IDS, "errors": errors}
            return watcher_run_public(run)

        readings = _thermal_readings(tool_payloads)
        successful_provider_ids = {
            str(reading.get("provider_id") or "")
            for reading in readings
            if reading.get("provider_id")
        }
        if not successful_provider_ids:
            run.status = "error"
            run.error = "no readable thermal sensors"
            run.finished_at = utcnow()
            run.payload = {
                "tool_ids": THERMAL_TOOL_IDS,
                "errors": errors,
                "readings": readings,
                "readings_total": 0,
            }
            return watcher_run_public(run)

        observed = _thermal_incidents(readings)
        detected = _filter_detected(watcher_id, observed)
        observed_keys = {incident.dedupe_key for incident in observed}
        actionable_keys = {incident.dedupe_key for incident in detected}
        await _refresh_filtered_open_incidents(
            db, observed, actionable_keys=actionable_keys, watcher_id=watcher_id
        )

        for incident in detected:
            result = await _upsert_incident(db, incident, actor=actor)
            if result.created:
                run.created_tasks += 1
            else:
                run.updated_incidents += 1

        resolved, clearing = await _resolve_missing_incidents(
            db,
            watcher_id=watcher_id,
            observed_keys=observed_keys,
            actor=actor,
            provider_ids=successful_provider_ids,
        )
        run.resolved_incidents = resolved
        run.status = "success"
        run.finished_at = utcnow()
        run.payload = {
            "tool_ids": THERMAL_TOOL_IDS,
            "errors": errors,
            "successful_provider_ids": sorted(successful_provider_ids),
            "readings": readings,
            "readings_total": len(readings),
            "findings_total": len(detected),
            "observed_total": len(observed),
            "clearing_total": clearing,
            "resolved_total": run.resolved_incidents,
            "thresholds": THERMAL_THRESHOLDS,
        }
        return watcher_run_public(run)
    except Exception as exc:
        logger.exception("watcher failed: %s", watcher_id)
        run.status = "error"
        run.error = exc.__class__.__name__
        run.finished_at = utcnow()
        run.payload = {"tool_ids": THERMAL_TOOL_IDS}
        return watcher_run_public(run)


async def _run_backup_freshness_watcher(db: AsyncSession, *, actor: Actor) -> dict[str, Any]:
    watcher_id = "backup.freshness"
    tool_ids = ["proxmox.backups.list", "pbs.backup.jobs.health"]
    run = WatcherRun(watcher_id=watcher_id, status="running")
    db.add(run)
    await db.flush()
    if not await _try_acquire_watcher_lock(db, watcher_id):
        run.status = "skipped"
        run.finished_at = utcnow()
        run.payload = {"reason": "watcher_already_running"}
        return watcher_run_public(run)

    try:
        payloads: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for tool_id in tool_ids:
            tool_result = await execute_tool(tool_id, {}, actor, source="watcher")
            if tool_result.ok:
                payloads[tool_id] = tool_result.result or {}
            else:
                errors[tool_id] = tool_result.error.message if tool_result.error else "tool failed"
        # One backup source may legitimately be absent (e.g. no PBS in this
        # lab); the run only fails when no source at all is readable.
        if not payloads:
            run.status = "error"
            run.error = "; ".join(errors.values())[:500]
            run.finished_at = utcnow()
            run.payload = {"tool_ids": tool_ids, "errors": errors}
            return watcher_run_public(run)

        successful_provider_ids = {
            provider_id
            for tool_id, provider_id in (
                ("proxmox.backups.list", "proxmox"),
                ("pbs.backup.jobs.health", "pbs"),
            )
            if tool_id in payloads
        }
        observed = _backup_freshness_incidents(
            payloads.get("proxmox.backups.list", {}),
            payloads.get("pbs.backup.jobs.health", {}),
        )
        detected = _filter_detected(watcher_id, observed)
        observed_keys = {incident.dedupe_key for incident in observed}
        actionable_keys = {incident.dedupe_key for incident in detected}
        await _refresh_filtered_open_incidents(
            db, observed, actionable_keys=actionable_keys, watcher_id=watcher_id
        )

        for incident in detected:
            result = await _upsert_incident(db, incident, actor=actor)
            if result.created:
                run.created_tasks += 1
            else:
                run.updated_incidents += 1

        resolved, clearing = await _resolve_missing_incidents(
            db,
            watcher_id=watcher_id,
            observed_keys=observed_keys,
            actor=actor,
            provider_ids=successful_provider_ids,
        )
        run.resolved_incidents = resolved
        run.status = "success"
        run.finished_at = utcnow()
        run.payload = {
            "tool_ids": tool_ids,
            "errors": errors,
            "findings_total": len(detected),
            "observed_total": len(observed),
            "clearing_total": clearing,
            "resolved_total": run.resolved_incidents,
        }
        return watcher_run_public(run)
    except Exception as exc:
        logger.exception("watcher failed: %s", watcher_id)
        run.status = "error"
        run.error = exc.__class__.__name__
        run.finished_at = utcnow()
        run.payload = {"tool_ids": tool_ids}
        return watcher_run_public(run)


async def _run_disk_health_watcher(db: AsyncSession, *, actor: Actor) -> dict[str, Any]:
    watcher_id = "storage.disks"
    tool_id = "proxmox.disks.temperatures"
    run = WatcherRun(watcher_id=watcher_id, status="running")
    db.add(run)
    await db.flush()
    if not await _try_acquire_watcher_lock(db, watcher_id):
        run.status = "skipped"
        run.finished_at = utcnow()
        run.payload = {"reason": "watcher_already_running"}
        return watcher_run_public(run)

    try:
        tool_result = await execute_tool(tool_id, {}, actor, source="watcher")
        if not tool_result.ok:
            message = tool_result.error.message if tool_result.error else "tool failed"
            run.status = "error"
            run.error = message
            run.finished_at = utcnow()
            run.payload = {"tool_id": tool_id, "error": message}
            return watcher_run_public(run)

        payload = tool_result.result or {}
        observed = _disk_health_incidents(payload)
        detected = _filter_detected(watcher_id, observed)
        observed_keys = {incident.dedupe_key for incident in observed}
        actionable_keys = {incident.dedupe_key for incident in detected}
        await _refresh_filtered_open_incidents(
            db, observed, actionable_keys=actionable_keys, watcher_id=watcher_id
        )

        for incident in detected:
            result = await _upsert_incident(db, incident, actor=actor)
            if result.created:
                run.created_tasks += 1
            else:
                run.updated_incidents += 1

        resolved, clearing = await _resolve_missing_incidents(
            db,
            watcher_id=watcher_id,
            observed_keys=observed_keys,
            actor=actor,
        )
        run.resolved_incidents = resolved
        run.status = "success"
        run.finished_at = utcnow()
        raw_disks = payload.get("disks")
        disks = raw_disks if isinstance(raw_disks, list) else []
        run.payload = {
            "tool_id": tool_id,
            # Keep every canonical health reading in the run payload so the
            # operator can review wearout trends before any threshold exists.
            "readings": _json_safe([
                {
                    "node": item.get("node"),
                    "devpath": item.get("devpath"),
                    "disk_model": item.get("disk_model"),
                    "health": item.get("health"),
                    "wearout": item.get("wearout"),
                }
                for item in disks
                if isinstance(item, dict)
            ]),
            "readings_total": len(disks),
            "findings_total": len(detected),
            "clearing_total": clearing,
            "resolved_total": run.resolved_incidents,
        }
        return watcher_run_public(run)
    except Exception as exc:
        logger.exception("watcher failed: %s", watcher_id)
        run.status = "error"
        run.error = exc.__class__.__name__
        run.finished_at = utcnow()
        run.payload = {"tool_id": tool_id}
        return watcher_run_public(run)


async def _upsert_incident(
    db: AsyncSession,
    detected: DetectedIncident,
    *,
    actor: Actor,
    root_incident_id: str | None = None,
) -> UpsertResult:
    existing = await _find_reusable_incident(db, detected)
    now = utcnow()
    skip_historical_match = False
    if existing is not None and await _retire_incident_with_inactive_root(
        db, existing, actor=actor, now=now
    ):
        skip_historical_match = True
        existing = None
    if existing is not None and await _retire_incident_with_final_task(
        db, existing, actor=actor, now=now
    ):
        skip_historical_match = True
        existing = None
    if existing is not None:
        return _refresh_and_link(existing, detected, root_incident_id, now=now)

    root = await db.get(Incident, root_incident_id) if root_incident_id else None
    if root is not None and root.status != "open":
        skip_historical_match = True
        root = None
    if root is not None and root.task_id:
        root_task = await db.get(Task, root.task_id)
        if root_task is not None and root_task.status in FINAL_TASK_STATUSES:
            if root.status == "open":
                await _retire_incident_with_final_task(db, root, actor=actor, now=now)
            # A root retired earlier in this same run is already resolved, so
            # the retirement helper returns False. It still must never lend
            # its final task to a new incident.
            skip_historical_match = True
            root = None
    match = IncidentMatchDecision(
        outcome="new", confidence=1.0, reason="correlated root task", method="deterministic"
    )
    if root is None and not skip_historical_match:
        match = await match_incident(db, detected, now=now)
        if match.outcome == "already_handled" and match.matched_incident_id:
            return await _record_already_handled_match(db, detected, match, actor=actor, now=now)
    try:
        task: Task | None = None
        async with db.begin_nested():
            if root is not None and root.task_id:
                # Correlated case: a graph-adjacent incident is already open
                # this run or from a prior one. Attach evidence to its task
                # instead of opening an independent one — this is the
                # literal "N findings, one task" behavior.
                task = await db.get(Task, root.task_id)
                if task is None:
                    raise RuntimeError("correlated root incident references an unavailable task")
                task_id = task.id
                effective_root = root.root_cause_incident_id or root.id
            else:
                task = await create_task(
                    db,
                    detected.title,
                    _task_goal(detected),
                    actor,
                    source="watcher",
                    notify=False,
                )
                task_id = task.id
                effective_root = None
            try:
                await add_finding(
                    db,
                    task_id,
                    detected.severity,
                    detected.title,
                    detected.description,
                    actor,
                    source="watcher",
                )
            except (TaskServiceError, SQLAlchemyError):
                logger.warning("watcher could not add task finding for incident %s", detected.dedupe_key)
            incident = Incident(
                dedupe_key=detected.dedupe_key,
                watcher_id=detected.watcher_id,
                status="open",
                severity=detected.severity,
                provider_id=detected.provider_id,
                title=detected.title,
                description=detected.description,
                task_id=task_id,
                first_seen_at=now,
                last_seen_at=now,
                missing_runs=0,
                last_missing_at=None,
                payload=_incident_payload(detected, "created_new_task" if effective_root is None else "linked_to_root_task"),
                root_cause_incident_id=effective_root,
            )
            db.add(incident)
            await db.flush()
            if effective_root is None:
                if task is None:
                    raise RuntimeError("new watcher incident was created without a task")
                await enqueue_watcher_incident(
                    db,
                    incident_id=incident.id,
                    dedupe_key=incident.dedupe_key,
                    severity=incident.severity,
                    provider_id=incident.provider_id,
                    title=incident.title,
                    task_id=task.id,
                    reply_markup=_task_keyboard(task.id),
                    watcher_id=incident.watcher_id,
                    group_key=_notification_group_key(detected),
                    immediate=(
                        incident.severity == "critical"
                        and incident.watcher_id in IMMEDIATE_CRITICAL_WATCHERS
                    ),
                )
                if match.outcome == "possible_match" or match.method in {"llm", "fallback"}:
                    await _record_match_decision(db, task, match, actor=actor)
                watcher_config = _watcher_config(detected.watcher_id)
                routing_job = await enqueue_task_routing(
                    db,
                    task,
                    actor,
                    source="watcher",
                    context={
                        "trigger": "watcher_incident",
                        "incident": incident_public(incident),
                        "finding": {
                            "severity": detected.severity,
                            "provider_id": detected.provider_id,
                            "title": detected.title,
                            "description": detected.description,
                            "dedupe_key": detected.dedupe_key,
                        },
                    },
                    policy_context={
                        "kind": "watcher_auto_investigate",
                        "watcher_id": detected.watcher_id,
                        "investigation_mode": watcher_config.investigation_mode,
                        "severity": detected.severity,
                        "runbook": WATCHER_RUNBOOKS.get(detected.watcher_id),
                        "match_outcome": match.outcome,
                        "match_method": match.method,
                    },
                )
                if routing_job is None:
                    await maybe_auto_investigate(
                        db,
                        task,
                        actor,
                        watcher_id=detected.watcher_id,
                        investigation_mode=watcher_config.investigation_mode,
                        severity=detected.severity,
                        runbook=WATCHER_RUNBOOKS.get(detected.watcher_id),
                        match_outcome=match.outcome,
                        match_method=match.method,
                        decision=None,
                    )
        created = effective_root is None
        return UpsertResult(
            created=created, incident_id=incident.id, effective_root_id=effective_root or incident.id
        )
    except IntegrityError:
        existing = await _find_reusable_incident(db, detected)
        if existing is None:
            raise
        if await _retire_incident_with_final_task(db, existing, actor=actor, now=now):
            return await _upsert_incident(db, detected, actor=actor, root_incident_id=None)
        return _refresh_and_link(existing, detected, root_incident_id, now=now)


async def _retire_incident_with_inactive_root(
    db: AsyncSession,
    incident: Incident,
    *,
    actor: Actor,
    now: datetime,
) -> bool:
    if incident.status != "open" or not incident.root_cause_incident_id:
        return False
    root = await db.get(Incident, incident.root_cause_incident_id)
    if root is not None and root.status == "open":
        return False
    incident.status = "resolved"
    incident.resolved_at = now
    incident.resolution_reason = "root_cause_cleared"
    incident.missing_runs = 0
    incident.last_missing_at = None
    await cancel_pending_for_incident(
        db, provider_id=incident.provider_id, dedupe_key=incident.dedupe_key
    )
    payload = {
        "incident_id": incident.id,
        "watcher_id": incident.watcher_id,
        "root_cause_incident_id": incident.root_cause_incident_id,
        "resolution_reason": "root_cause_cleared",
    }
    if incident.task_id:
        db.add(
            TaskEvent(
                task_id=incident.task_id,
                kind="watcher.incident.root_cause_cleared",
                payload=payload,
            )
        )
    await write_audit(
        db,
        actor=actor,
        source="watcher",
        action="watcher.incident.root_cause_cleared",
        outcome="reconciled",
        task_id=incident.task_id or "",
        metadata=payload,
    )
    return True


async def _retire_incident_with_final_task(
    db: AsyncSession,
    incident: Incident,
    *,
    actor: Actor,
    now: datetime,
) -> bool:
    if incident.status != "open" or not incident.task_id:
        return False
    task = await db.get(Task, incident.task_id)
    if task is None or task.status not in FINAL_TASK_STATUSES:
        return False
    incident.status = "resolved"
    incident.resolved_at = now
    incident.resolution_reason = "task_finalized"
    incident.missing_runs = 0
    incident.last_missing_at = None
    payload = {
        "incident_id": incident.id,
        "watcher_id": incident.watcher_id,
        "task_status": task.status,
        "resolution_reason": "task_finalized",
    }
    db.add(TaskEvent(task_id=task.id, kind="watcher.incident.task_finalized", payload=payload))
    await write_audit(
        db,
        actor=actor,
        source="watcher",
        action="watcher.incident.task_finalized",
        outcome="reconciled",
        task_id=task.id,
        metadata=payload,
    )
    return True


async def _record_already_handled_match(
    db: AsyncSession,
    detected: DetectedIncident,
    match: IncidentMatchDecision,
    *,
    actor: Actor,
    now: datetime,
) -> UpsertResult:
    previous = await db.get(Incident, match.matched_incident_id)
    if previous is None or previous.task_id is None:
        raise RuntimeError("incident matcher selected an unavailable incident")
    incident = Incident(
        dedupe_key=detected.dedupe_key,
        watcher_id=detected.watcher_id,
        status="resolved",
        severity=detected.severity,
        provider_id=detected.provider_id,
        title=detected.title,
        description=detected.description,
        task_id=previous.task_id,
        first_seen_at=now,
        last_seen_at=now,
        resolved_at=now,
        resolution_reason="operator_already_handled",
        payload={
            **_incident_payload(detected, "matched_already_handled"),
            "matched_incident_id": previous.id,
            "match_confidence": match.confidence,
            "match_method": match.method,
        },
    )
    db.add(incident)
    await db.flush()
    task = await db.get(Task, previous.task_id)
    if task is None:
        raise RuntimeError("incident matcher selected an incident with an unavailable task")
    await _record_match_decision(db, task, match, actor=actor, auto_handled_incident_id=incident.id)
    return UpsertResult(created=False, incident_id=incident.id, effective_root_id=incident.id)


async def _record_match_decision(
    db: AsyncSession,
    task: Task,
    match: IncidentMatchDecision,
    *,
    actor: Actor,
    auto_handled_incident_id: str | None = None,
) -> None:
    payload = {
        "decision": match.model_dump(mode="json", exclude={"telemetry"}),
        "auto_handled_incident_id": auto_handled_incident_id,
    }
    kind = "watcher.incident.auto_matched" if auto_handled_incident_id else "watcher.incident.possible_match"
    event = TaskEvent(task_id=task.id, kind=kind, payload=redact(payload))
    db.add(event)
    await db.flush()
    if match.model:
        await record_llm_usage(
            db,
            component="incident_matcher",
            model=match.model,
            status="error" if match.method == "fallback" else "success",
            input_tokens=match.input_tokens,
            output_tokens=match.output_tokens,
            task_id=task.id,
            reference_id=event.id,
            **match.telemetry,
        )
    await write_audit(
        db,
        actor=actor,
        source="watcher",
        action="watcher.incident_matcher",
        outcome="success",
        task_id=task.id,
        metadata=redact(payload),
    )


def _refresh_and_link(
    existing: Incident,
    detected: DetectedIncident,
    root_incident_id: str | None,
    *,
    now,
) -> UpsertResult:
    _refresh_incident(existing, detected, now=now)
    # A previously-independent incident can become correlated once the
    # graph reveals a root; never move an already-linked incident, and
    # never retroactively move its task_id — only future task creation is
    # prevented, existing task history stays put.
    if root_incident_id and root_incident_id != existing.id and not existing.root_cause_incident_id:
        existing.root_cause_incident_id = root_incident_id
    return UpsertResult(
        created=False,
        incident_id=existing.id,
        effective_root_id=existing.root_cause_incident_id or existing.id,
    )


async def _watcher_status_items(
    db: AsyncSession | None, *, automation: bool
) -> list[dict[str, Any]]:
    if db is not None:
        await _load_watcher_configs(db)
    latest_by_id: dict[str, WatcherRun] = {}
    if db is not None:
        rows = (
            await db.execute(select(WatcherRun).order_by(WatcherRun.started_at.desc()).limit(100))
        ).scalars()
        for run in rows:
            latest_by_id.setdefault(run.watcher_id, run)

    result = []
    for watcher_id in sorted(WATCHER_IDS):
        config = _watcher_config(watcher_id)
        latest = latest_by_id.get(watcher_id)
        last_error = latest.error if latest and latest.status == "error" else ""
        next_run = None
        if automation and config.enabled:
            base = _aware_utc(latest.started_at) if latest else utcnow()
            next_run = base + timedelta(seconds=config.interval_seconds)
        result.append(
            {
                "id": watcher_id,
                "label": WATCHER_LABELS.get(watcher_id, watcher_id),
                "enabled": config.enabled,
                "interval_seconds": config.interval_seconds,
                "min_severity": config.min_severity,
                "investigation_mode": config.investigation_mode,
                "last_run": watcher_run_public(latest) if latest else None,
                "last_error": last_error,
                "next_run_at": next_run,
                "runbook_incident_type": WATCHER_RUNBOOKS.get(watcher_id),
            }
        )
    return result


def _watcher_config(watcher_id: str) -> WatcherRuntimeConfig:
    settings = get_settings()
    patch = _watcher_overrides.get(watcher_id, {})
    enabled = bool(patch.get("enabled", watcher_id in DEFAULT_WATCHER_IDS))
    interval = int(patch.get("interval_seconds", max(60, settings.watchers_interval_seconds)))
    min_severity = str(patch.get("min_severity", _minimum_severity())).lower()
    if min_severity not in {"warning", "critical"}:
        min_severity = "warning"
    investigation_mode = str(patch.get("investigation_mode", "manual")).lower()
    if investigation_mode not in {"manual", "auto_investigate"}:
        investigation_mode = "manual"
    return WatcherRuntimeConfig(
        watcher_id=watcher_id,
        enabled=enabled,
        interval_seconds=max(60, interval),
        min_severity=min_severity,
        investigation_mode=investigation_mode,
    )


async def _load_watcher_configs(db: AsyncSession) -> None:
    rows = (await db.execute(select(WatcherConfig))).scalars().all()
    for row in rows:
        if row.watcher_id not in WATCHER_IDS:
            continue
        _watcher_overrides[row.watcher_id] = {
            "enabled": row.enabled,
            "interval_seconds": row.interval_seconds,
            "min_severity": row.min_severity,
            "investigation_mode": row.investigation_mode,
        }


async def _load_watcher_automation_enabled(db: AsyncSession) -> bool:
    row = await db.get(WatcherAutomationState, "global")
    return row.enabled if row is not None else get_settings().watchers_enabled


def _enabled_watcher_ids() -> set[str]:
    return {watcher_id for watcher_id in WATCHER_IDS if _watcher_config(watcher_id).enabled}


def _filter_detected(watcher_id: str, detected: list[DetectedIncident]) -> list[DetectedIncident]:
    minimum = _watcher_config(watcher_id).min_severity
    return [
        incident for incident in detected
        if SEVERITY_RANK.get(incident.severity, 0) >= SEVERITY_RANK[minimum]
    ]


async def _refresh_filtered_open_incidents(
    db: AsyncSession,
    observed: list[DetectedIncident],
    *,
    actionable_keys: set[str],
    watcher_id: str,
) -> None:
    """Refresh existing incidents that are present but muted by policy.

    Filtered observations never create a task, but they remain evidence that
    an already-open incident has not cleared.
    """
    filtered = {
        incident.dedupe_key: incident
        for incident in observed
        if incident.dedupe_key not in actionable_keys
    }
    if not filtered:
        return
    rows = (
        await db.execute(
            select(Incident).where(
                Incident.watcher_id == watcher_id,
                Incident.status == "open",
                Incident.dedupe_key.in_(filtered),
            )
        )
    ).scalars()
    now = utcnow()
    for incident in rows:
        detected = filtered[incident.dedupe_key]
        _refresh_incident(incident, detected, now=now)
        payload = dict(incident.payload or {})
        payload["policy_state"] = "filtered"
        payload["filter_reason"] = _incident_filter_reason(watcher_id, detected)
        incident.payload = _json_safe(redact(payload))


async def _resolve_missing_incidents(
    db: AsyncSession,
    *,
    watcher_id: str,
    observed_keys: set[str],
    actor: Actor,
    provider_ids: set[str] | None = None,
) -> tuple[int, int]:
    query = select(Incident).where(
        Incident.watcher_id == watcher_id, Incident.status == "open"
    )
    if provider_ids is not None:
        if not provider_ids:
            return 0, 0
        query = query.where(Incident.provider_id.in_(provider_ids))
    rows = (await db.execute(query)).scalars().all()
    now = utcnow()
    resolved = 0
    clearing = 0
    resolve_after = _resolve_after_missing_runs()
    for incident in rows:
        if incident.dedupe_key in observed_keys:
            continue
        incident.missing_runs += 1
        incident.last_missing_at = now
        if incident.missing_runs < resolve_after:
            clearing += 1
            continue
        incident.status = "resolved"
        incident.resolved_at = now
        incident.resolution_reason = "alert_cleared"
        await cancel_pending_for_incident(
            db, provider_id=incident.provider_id, dedupe_key=incident.dedupe_key
        )
        await _maybe_auto_complete_cleared_task(db, incident=incident, actor=actor, now=now)
        resolved += 1
    return resolved, clearing


def _topo_order(detected: list[DetectedIncident], present: set[str]) -> list[DetectedIncident]:
    """Stable sort by count of present upstream ancestors (fewer first) — a
    cheap valid topological order for the shallow dependency graph this
    models, so an upstream provider's root_by_provider entry is resolved
    before its dependents are processed within the same run."""
    return sorted(
        detected,
        key=lambda d: len(set(dependency_graph.upstream_of(d.provider_id)) & present),
    )


def _notification_group_key(detected: DetectedIncident) -> str:
    if detected.watcher_id == "backup.freshness":
        return "coverage:backup"
    if detected.watcher_id == "security.certificates":
        return "security:tls"
    if detected.watcher_id not in CONNECTIVITY_WATCHERS:
        return ""
    if (
        detected.watcher_id == "lab.alerts"
        and detected.payload.get("code") != "provider_health"
    ):
        return ""
    upstream = dependency_graph.upstream_of(detected.provider_id)
    candidates = [detected.provider_id, *upstream]
    roots = [item for item in candidates if not dependency_graph.upstream_of(item)]
    root = sorted(roots)[0] if roots else detected.provider_id
    return f"topology:{root}"


def _pick_root_candidate(
    provider_id: str,
    correlated_ids: set[str],
    root_by_provider: dict[str, str],
    *,
    rootable_ids: set[str] | None = None,
) -> str | None:
    """Among provider_id's upstream ancestors that are active this run or
    already open from a prior run, pick the topmost one (fewest other
    candidates above it) and return its resolved effective-root incident
    id. None if no ancestor qualifies — today's independent-incident path.

    If rootable_ids is provided, only those ancestors may become roots. This
    keeps low-confidence warning symptoms from swallowing unrelated downstream
    incidents simply because the static topology says they are upstream.
    """
    ancestors = [a for a in dependency_graph.upstream_of(provider_id) if a in correlated_ids]
    if rootable_ids is not None:
        ancestors = [a for a in ancestors if a in rootable_ids]
    if not ancestors:
        return None
    ancestor_set = set(ancestors)
    topmost = min(ancestors, key=lambda a: len(set(dependency_graph.upstream_of(a)) & ancestor_set))
    return root_by_provider.get(topmost)


async def _open_incident_provider_ids(db: AsyncSession, watcher_id: str) -> dict[str, str]:
    """provider_id -> effective root incident id, seeded from incidents
    already open from a prior run (root_cause_incident_id if set, else the
    incident's own id)."""
    rows = (
        await db.execute(
            select(Incident.provider_id, Incident.id, Incident.root_cause_incident_id).where(
                Incident.watcher_id == watcher_id, Incident.status == "open"
            )
        )
    ).all()
    return {provider_id: (root_id or incident_id) for provider_id, incident_id, root_id in rows}


async def _open_critical_root_provider_ids(db: AsyncSession, watcher_id: str) -> set[str]:
    rows = (
        await db.execute(
            select(Incident.provider_id).where(
                Incident.watcher_id == watcher_id,
                Incident.status == "open",
                Incident.severity == "critical",
                Incident.root_cause_incident_id.is_(None),
            )
        )
    ).scalars().all()
    return set(rows)


async def _find_reusable_incident(db: AsyncSession, detected: DetectedIncident) -> Incident | None:
    exact = (
        await db.execute(
            select(Incident)
            .where(
                Incident.dedupe_key == detected.dedupe_key,
                (
                    (Incident.status == "open")
                    | (
                        (Incident.status == "resolved")
                        & (Incident.resolution_reason == "operator_already_handled")
                    )
                ),
            )
            .order_by(Incident.last_seen_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if exact is not None:
        return exact

    candidates = (
        await db.execute(
            select(Incident)
            .where(
                Incident.watcher_id == detected.watcher_id,
                Incident.provider_id == detected.provider_id,
                Incident.status == "open",
            )
            .order_by(Incident.last_seen_at.desc())
            .limit(20)
        )
    ).scalars()
    for incident in candidates:
        if _dedupe_basis(incident.description) == detected.dedupe_basis:
            return incident
    return None


async def _latest_success_payload(db: AsyncSession, watcher_id: str) -> dict[str, Any]:
    row = (
        await db.execute(
            select(WatcherRun)
            .where(WatcherRun.watcher_id == watcher_id, WatcherRun.status == "success")
            .order_by(WatcherRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row.payload if row is not None and isinstance(row.payload, dict) else {}


def _network_presence_snapshot(arp_devices: Any, leases: Any) -> dict[str, Any]:
    arp_rows = arp_devices if isinstance(arp_devices, list) else []
    lease_rows = leases if isinstance(leases, list) else []
    devices: dict[str, dict[str, Any]] = {}
    arp_bindings: dict[str, set[str]] = {}
    kea_bindings: dict[str, set[str]] = {}

    for row in arp_rows:
        if not isinstance(row, dict):
            continue
        mac = _normalize_mac(row.get("mac_address"))
        ip = str(row.get("ip_address") or "").strip()
        if not mac and not ip:
            continue
        key = mac or f"ip:{ip}"
        entry = devices.setdefault(key, {"mac": mac, "ips": set(), "hostnames": set(), "sources": set()})
        if ip:
            entry["ips"].add(ip)
            if mac:
                arp_bindings.setdefault(ip, set()).add(mac)
        hostname = str(row.get("hostname") or "").strip()
        if hostname:
            entry["hostnames"].add(hostname)
        entry["sources"].add("arp")

    for row in lease_rows:
        if not isinstance(row, dict):
            continue
        if not _kea_lease_is_active(row):
            continue
        mac = _normalize_mac(row.get("mac_address"))
        ip = str(row.get("ip_address") or "").strip()
        if not mac and not ip:
            continue
        key = mac or f"ip:{ip}"
        entry = devices.setdefault(key, {"mac": mac, "ips": set(), "hostnames": set(), "sources": set()})
        if ip:
            entry["ips"].add(ip)
            if mac:
                kea_bindings.setdefault(ip, set()).add(mac)
        hostname = str(row.get("hostname") or "").strip()
        if hostname:
            entry["hostnames"].add(hostname)
        entry["sources"].add("kea")

    observed_macs = {entry["mac"] for entry in devices.values() if entry["mac"]}
    observed_ips = {ip for entry in devices.values() for ip in entry["ips"]}
    serializable = []
    for key, entry in sorted(devices.items()):
        serializable.append(
            {
                "key": key,
                "mac": entry["mac"],
                "ips": sorted(entry["ips"]),
                "hostnames": sorted(entry["hostnames"]),
                "sources": sorted(entry["sources"]),
            }
        )
    return {
        "devices": serializable,
        "observed_macs": observed_macs,
        "observed_ips": observed_ips,
        "arp_bindings": arp_bindings,
        "kea_bindings": kea_bindings,
    }


def _kea_lease_is_active(row: dict[str, Any]) -> bool:
    """Treat only current Kea leases as network-presence evidence.

    Kea uses 0/default for active leases, 1 for declined/inactive and 2 for
    expired-reclaimed. Some normalized/fallback endpoints omit state, so an
    otherwise valid row remains usable unless its lifetime is explicitly zero.
    """
    state = str(row.get("state") or "").strip().lower().replace("_", "-")
    inactive_states = {
        "1",
        "2",
        "inactive",
        "declined",
        "expired",
        "expired-reclaimed",
        "reclaimed",
    }
    if state in inactive_states:
        return False
    lifetime = row.get("valid_lifetime_seconds")
    try:
        if lifetime is not None and float(lifetime) <= 0:
            return False
    except (TypeError, ValueError):
        pass
    return state in {"", "0", "active", "default"}


def _network_presence_incidents(snapshot: dict[str, Any], previous_macs: set[str]) -> list[DetectedIncident]:
    incidents: list[DetectedIncident] = []
    devices = snapshot["devices"]
    observed_macs = snapshot["observed_macs"]
    new_macs = sorted(observed_macs - previous_macs)
    for mac in new_macs[:20]:
        device = next((item for item in devices if item.get("mac") == mac), {})
        label = _device_label(device)
        incidents.append(
            _network_incident(
                "warning",
                "new_device",
                f"New network device observed: {label}",
                {"mac": mac, "device": device},
            )
        )

    arp_bindings = snapshot.get("arp_bindings", {})
    kea_bindings = snapshot.get("kea_bindings", {})
    binding_ips = set(arp_bindings) | set(kea_bindings)
    duplicate_ips = sorted(
        ip
        for ip in binding_ips
        if len(arp_bindings.get(ip, set())) > 1 or len(kea_bindings.get(ip, set())) > 1
    )
    for ip in duplicate_ips[:10]:
        matched = [device for device in devices if ip in device.get("ips", [])]
        incidents.append(
            _network_incident(
                "critical",
                "duplicate_ip",
                f"Duplicate IP observed on network: {ip}",
                {
                    "ip": ip,
                    "devices": matched,
                    "arp_macs": sorted(arp_bindings.get(ip, set())),
                    "kea_macs": sorted(kea_bindings.get(ip, set())),
                },
            )
        )

    mismatched_ips = sorted(
        ip
        for ip in binding_ips - set(duplicate_ips)
        if arp_bindings.get(ip) and kea_bindings.get(ip)
        and arp_bindings[ip] != kea_bindings[ip]
    )
    for ip in mismatched_ips[:10]:
        matched = [device for device in devices if ip in device.get("ips", [])]
        incidents.append(
            _network_incident(
                "warning",
                "arp_kea_mismatch",
                f"ARP and DHCP disagree on the device using {ip}",
                {
                    "ip": ip,
                    "devices": matched,
                    "arp_macs": sorted(arp_bindings.get(ip, set())),
                    "kea_macs": sorted(kea_bindings.get(ip, set())),
                },
            )
        )

    hostname_counts = Counter(
        hostname.lower()
        for device in devices
        for hostname in device.get("hostnames", [])
        if hostname
    )
    duplicate_hostnames = sorted(hostname for hostname, count in hostname_counts.items() if count > 1)
    for hostname in duplicate_hostnames[:10]:
        matched = [
            device
            for device in devices
            if hostname in [str(item).lower() for item in device.get("hostnames", [])]
        ]
        incidents.append(
            _network_incident(
                "warning",
                "duplicate_hostname",
                f"Duplicate hostname observed on network: {hostname}",
                {"hostname": hostname, "devices": matched},
            )
        )

    arp_only = [
        device
        for device in devices
        if device.get("mac") and "arp" in device.get("sources", []) and "kea" not in device.get("sources", [])
    ]
    if len(arp_only) >= 5:
        incidents.append(
            _network_incident(
                "warning",
                "arp_without_kea",
                f"{len(arp_only)} ARP device(s) are not present in Kea leases",
                {"devices": arp_only[:25], "total": len(arp_only)},
            )
        )
    return incidents


def _network_incident(
    severity: str,
    code: str,
    message: str,
    payload: dict[str, Any],
    *,
    watcher_id: str = "network.presence",
) -> DetectedIncident:
    basis = code
    if code == "new_device":
        basis = f"{code}:{payload.get('mac', '')}"
    elif "availability_group" in payload and payload.get("availability_group"):
        basis = f"{code}:{payload.get('availability_group', '')}"
    elif "gateway" in payload:
        basis = f"{code}:{payload.get('gateway', '')}"
    elif "ip" in payload:
        basis = f"{code}:{payload.get('ip', '')}"
    elif "hostname" in payload:
        basis = f"{code}:{payload.get('hostname', '')}"
    return DetectedIncident(
        watcher_id=watcher_id,
        dedupe_key=_dedupe_key(watcher_id, "opnsense", basis),
        dedupe_basis=basis,
        severity=severity,
        provider_id="opnsense",
        title=f"[Network Watcher] {message}"[:256],
        description=message[:4000],
        payload=_json_safe(redact({"code": code, **payload})),
    )


def _network_gateway_incidents(payload: dict[str, Any]) -> list[DetectedIncident]:
    raw_gateways = payload.get("gateways")
    gateways = raw_gateways if isinstance(raw_gateways, list) else []
    rows = [item for item in gateways if isinstance(item, dict)]
    policies = _gateway_watch_policies(rows)
    incidents: list[DetectedIncident] = []
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any] | None]]] = {}
    ungrouped: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    by_name = {
        str(item.get("name") or "").strip().casefold(): item
        for item in rows
        if str(item.get("name") or "").strip()
    }

    for policy in policies:
        gateway = by_name.get(policy["gateway_name"].casefold())
        item = (policy, gateway)
        if policy["availability_group"]:
            grouped.setdefault(policy["availability_group"], []).append(item)
        else:
            ungrouped.append(item)

    group_unavailable: set[str] = set()
    for group_id, items in grouped.items():
        present = [gateway for _, gateway in items if gateway is not None]
        online = [gateway for gateway in present if gateway.get("online") is True]
        group_mode = str(items[0][0].get("group_mode") or "all")
        unavailable = (
            bool(present)
            and len(present) == len(items)
            and (not online if group_mode == "any" else len(online) < len(items))
        )
        if unavailable:
            group_unavailable.add(group_id)
            observation_ids = [
                str(policy.get("observation_id") or "")
                for policy, _ in items
                if policy.get("observation_id")
            ]
            incidents.append(
                _network_incident(
                    "critical",
                    "gateway_group_unavailable",
                    f"OPNsense WAN group unavailable: {group_id}",
                    {
                        "availability_group": group_id,
                        "observation_ids": observation_ids,
                        "gateways": [policy["gateway_name"] for policy, _ in items],
                    },
                    watcher_id="network.gateway",
                )
            )

    for policy, gateway in [*ungrouped, *(item for items in grouped.values() for item in items)]:
        name = policy["gateway_name"]
        observation_id = str(policy.get("observation_id") or "")
        group_id = str(policy.get("availability_group") or "")
        scoped_payload = {
            "gateway": name,
            "observation_id": observation_id,
            "availability_group": group_id,
        }
        if gateway is None:
            incidents.append(
                _network_incident(
                    "warning",
                    "gateway_missing",
                    f"Configured OPNsense gateway was not returned: {name}",
                    scoped_payload,
                    watcher_id="network.gateway",
                )
            )
            continue
        if gateway.get("online") is not True:
            if group_id in group_unavailable:
                continue
            incidents.append(
                _network_incident(
                    "warning" if group_id else "critical",
                    "gateway_offline",
                    f"OPNsense gateway offline: {name}",
                    {**scoped_payload, "details": gateway},
                    watcher_id="network.gateway",
                )
            )
            continue
        if not policy["performance_monitoring"]:
            continue
        loss = _as_float(gateway.get("loss_percent"))
        rtt = _as_float(gateway.get("rtt_ms"))
        jitter = _as_float(gateway.get("rtt_stddev_ms"))
        loss_critical = float(policy["loss_critical_percent"])
        loss_warning = float(policy["loss_warning_percent"])
        rtt_warning = float(policy["rtt_warning_ms"])
        jitter_warning = float(policy["jitter_warning_ms"])
        reasons: list[str] = []
        severity = "warning"
        if loss is not None and loss >= loss_critical:
            severity = "critical"
            reasons.append(f"loss {loss:g}%")
        elif loss is not None and loss >= loss_warning:
            reasons.append(f"loss {loss:g}%")
        if rtt is not None and rtt >= rtt_warning:
            reasons.append(f"latency {rtt:g} ms")
        if jitter is not None and jitter >= jitter_warning:
            reasons.append(f"jitter {jitter:g} ms")
        if reasons:
            incidents.append(
                _network_incident(
                    severity,
                    "gateway_performance",
                    f"OPNsense gateway performance degraded: {name} ({', '.join(reasons)})",
                    {
                        **scoped_payload,
                        "loss_percent": loss,
                        "rtt_ms": rtt,
                        "rtt_stddev_ms": jitter,
                        "details": gateway,
                    },
                    watcher_id="network.gateway",
                )
            )
    return incidents


def _gateway_watch_policies(gateways: list[dict[str, Any]]) -> list[dict[str, Any]]:
    configured = inventory.provider_config("opnsense").get("gateway_observations", [])
    items = configured if isinstance(configured, list) else []
    nodes_by_observation = {
        node.observation_id: node
        for node in inventory.list_topology_nodes()
        if node.observation_id
    }
    edges_by_source = {
        edge.source: edge
        for edge in inventory.list_topology_edges()
        if edge.availability_group
    }
    group_modes = {
        group.id: group.mode for group in inventory.list_topology_availability_groups()
    }
    policies: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        gateway_name = str(item.get("gateway_name") or "").strip()
        observation_suffix = str(item.get("id") or "").strip().lower()
        if not gateway_name or not observation_suffix:
            continue
        observation_id = f"opnsense.gateway.{observation_suffix}"
        node = nodes_by_observation.get(observation_id)
        edge = edges_by_source.get(node.id) if node else None
        group_id = edge.availability_group if edge else ""
        policies.append(
            {
                "gateway_name": gateway_name,
                "observation_id": observation_id,
                "availability_group": group_id,
                "group_mode": group_modes.get(group_id, "all"),
                "performance_monitoring": item.get("performance_monitoring") is not False,
                "loss_warning_percent": _bounded_threshold(item.get("loss_warning_percent"), 10),
                "loss_critical_percent": _bounded_threshold(item.get("loss_critical_percent"), 50),
                "rtt_warning_ms": _bounded_threshold(item.get("rtt_warning_ms"), 150),
                "jitter_warning_ms": _bounded_threshold(item.get("jitter_warning_ms"), 30),
            }
        )
    if policies:
        return policies
    return [
        {
            "gateway_name": str(item.get("name") or item.get("address") or "unknown"),
            "observation_id": "",
            "availability_group": "",
            "group_mode": "all",
            "performance_monitoring": True,
            "loss_warning_percent": 10.0,
            "loss_critical_percent": 50.0,
            "rtt_warning_ms": 150.0,
            "jitter_warning_ms": 30.0,
        }
        for item in gateways
    ]


def _bounded_threshold(value: Any, default: float) -> float:
    number = _as_float(value)
    return default if number is None or number < 0 else min(number, 1_000_000)


def _network_zerotier_incidents(payload: dict[str, Any]) -> list[DetectedIncident]:
    required_total = int(payload.get("required_total") or 0)
    required_online = int(payload.get("required_online") or 0)
    required_unavailable = int(payload.get("required_unavailable") or 0)
    if not required_total or not required_unavailable:
        return []
    severity = "critical" if required_online == 0 else "warning"
    message = (
        "No required ZeroTier members are online"
        if required_online == 0
        else f"{required_online} of {required_total} required ZeroTier members are online"
    )
    basis = "required_members_unavailable"
    return [
        DetectedIncident(
            watcher_id="network.zerotier",
            dedupe_key=_dedupe_key("network.zerotier", "zerotier", basis),
            dedupe_basis=basis,
            severity=severity,
            provider_id="zerotier",
            title=f"[ZeroTier Watcher] {message}"[:256],
            description=message,
            payload=_json_safe(
                {
                    "code": basis,
                    "observation_id": "zerotier.members",
                    "required_total": required_total,
                    "required_online": required_online,
                    "required_unavailable": required_unavailable,
                }
            ),
        )
    ]


def _cloudflare_tunnel_incidents(payload: dict[str, Any]) -> list[DetectedIncident]:
    raw_summary = payload.get("summary")
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    raw_metrics = summary.get("metrics")
    metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    unavailable = int(metrics.get("tunnels_unavailable") or 0)
    degraded = int(metrics.get("tunnels_degraded") or 0)
    active = int(metrics.get("connections_active") or 0)
    pending = int(metrics.get("connections_pending_reconnect") or 0)
    if unavailable:
        severity, code, message = (
            "critical",
            "tunnel_unavailable",
            f"{unavailable} Cloudflare tunnel(s) are unavailable",
        )
    elif degraded:
        severity, code, message = (
            "warning",
            "tunnel_degraded",
            f"{degraded} Cloudflare tunnel(s) are degraded",
        )
    elif active == 0 and int(metrics.get("tunnels_total") or 0):
        severity, code, message = (
            "critical",
            "no_active_connections",
            "No active cloudflared connections",
        )
    elif pending:
        severity, code, message = (
            "warning",
            "connections_pending_reconnect",
            f"{pending} cloudflared connection(s) are pending reconnect",
        )
    else:
        return []
    return [
        DetectedIncident(
            watcher_id="cloudflare.tunnel",
            dedupe_key=_dedupe_key("cloudflare.tunnel", "cloudflaretunnel", code),
            dedupe_basis=code,
            severity=severity,
            provider_id="cloudflaretunnel",
            title=f"[Cloudflare Watcher] {message}"[:256],
            description=message,
            payload=_json_safe(
                {
                    "code": code,
                    "observation_id": "cloudflaretunnel.tunnel",
                    "metrics": metrics,
                }
            ),
        )
    ]


def _network_wireguard_incidents(payload: dict[str, Any]) -> list[DetectedIncident]:
    incidents: list[DetectedIncident] = []
    peers_total = int(payload.get("peers_total") or 0)
    peers_connected = int(payload.get("peers_connected") or 0)
    stale = [str(item) for item in payload.get("peers_stale", [])] if isinstance(payload.get("peers_stale"), list) else []
    if peers_total > 0 and peers_connected == 0:
        incidents.append(
            _network_incident(
                "critical",
                "wireguard_no_connected_peers",
                "No OPNsense WireGuard peers are connected",
                {"peers_total": peers_total, "peers_connected": peers_connected, "peers_stale": stale},
                watcher_id="network.wireguard",
            )
        )
    elif stale:
        incidents.append(
            _network_incident(
                "warning",
                "wireguard_stale_peers",
                f"OPNsense WireGuard stale peer(s): {', '.join(stale[:5])}",
                {"peers_total": peers_total, "peers_connected": peers_connected, "peers_stale": stale},
                watcher_id="network.wireguard",
            )
        )
    return incidents


def _uptimekuma_monitor_incidents(payload: dict[str, Any]) -> list[DetectedIncident]:
    incidents: list[DetectedIncident] = []
    raw_monitors = payload.get("monitors")
    monitors = raw_monitors if isinstance(raw_monitors, list) else []
    for monitor in monitors:
        if not isinstance(monitor, dict):
            continue
        status = str(monitor.get("status") or "unknown").strip().lower()
        if status in {"up", "maintenance"}:
            continue
        name = str(monitor.get("name") or monitor.get("target") or "unknown").strip()
        target = str(monitor.get("target") or "").strip()
        monitor_type = str(monitor.get("type") or "").strip()
        basis = f"{name}|{target}|{monitor_type}".lower()
        severity = "critical" if status == "down" else "warning"
        label = f"{name} ({target})" if target else name
        message = f"Uptime Kuma monitor {status}: {label}"
        incidents.append(
            DetectedIncident(
                watcher_id="uptimekuma.monitors",
                dedupe_key=_dedupe_key("uptimekuma.monitors", "uptimekuma", basis),
                dedupe_basis=basis,
                severity=severity,
                provider_id="uptimekuma",
                title=f"[Uptime Kuma] {message}"[:256],
                description=message[:4000],
                payload=_json_safe(redact({"code": "monitor_status", "monitor": monitor, "status": status})),
            )
        )
    return incidents


def _power_ups_incidents(payload: dict[str, Any]) -> list[DetectedIncident]:
    ups = payload.get("ups") if isinstance(payload.get("ups"), dict) else {}
    if not ups:
        return []
    name = str(ups.get("name") or "ups").strip()
    status = str(ups.get("status") or "unknown").strip().lower()
    flags = ups.get("status_flags") if isinstance(ups.get("status_flags"), list) else []
    charge = _as_float(ups.get("battery_charge_percent"))
    runtime = _as_float(ups.get("battery_runtime_seconds"))
    load = _as_float(ups.get("load_percent"))
    model = str(ups.get("model") or "").strip()

    incidents: list[DetectedIncident] = []
    if status in {"on_battery", "low_battery"}:
        incidents.append(
            _ups_incident(
                "critical",
                f"ups_{status}",
                f"NUT UPS {name} is {status.replace('_', ' ')}",
                {"ups": name, "status": status, "flags": flags, "charge": charge, "runtime": runtime, "load": load, "model": model},
            )
        )
    elif status in {"replace_battery", "alarm"}:
        incidents.append(
            _ups_incident(
                "warning",
                f"ups_{status}",
                f"NUT UPS {name} reports {status.replace('_', ' ')}",
                {"ups": name, "status": status, "flags": flags, "charge": charge, "runtime": runtime, "load": load, "model": model},
            )
        )
    if charge is not None and charge < 40:
        incidents.append(
            _ups_incident(
                "warning",
                "ups_low_charge",
                f"NUT UPS {name} battery charge is low: {charge:g}%",
                {"ups": name, "status": status, "charge": charge, "runtime": runtime, "load": load, "model": model},
            )
        )
    if runtime is not None and runtime < 600:
        incidents.append(
            _ups_incident(
                "warning",
                "ups_low_runtime",
                f"NUT UPS {name} runtime is below 10 minutes",
                {"ups": name, "status": status, "charge": charge, "runtime": runtime, "load": load, "model": model},
            )
        )
    return incidents


def _ups_incident(severity: str, code: str, message: str, payload: dict[str, Any]) -> DetectedIncident:
    ups_name = str(payload.get("ups") or "ups")
    basis = f"{code}:{ups_name}"
    return DetectedIncident(
        watcher_id="power.ups",
        dedupe_key=_dedupe_key("power.ups", "nutups", basis),
        dedupe_basis=basis,
        severity=severity,
        provider_id="nutups",
        title=f"[UPS Watcher] {message}"[:256],
        description=message[:4000],
        payload=_json_safe(redact({"code": code, **payload})),
    )


def _thermal_readings(payloads: dict[str, Any]) -> list[dict[str, Any]]:
    readings: list[dict[str, Any]] = []
    readings.extend(_thermal_ups_readings(payloads.get("nutups.status", {})))
    readings.extend(_thermal_opnsense_readings(payloads.get("opnsense.system.temperature", {})))
    readings.extend(_thermal_mikrotik_readings(payloads.get("mikrotik.system.health", {})))
    readings.extend(_thermal_fritzbox_readings("fritzbox_primary", payloads.get("fritzbox.primary.temperature", {})))
    readings.extend(_thermal_fritzbox_readings("fritzbox_secondary", payloads.get("fritzbox.secondary.temperature", {})))
    readings.extend(_thermal_proxmox_disk_readings(payloads.get("proxmox.disks.temperatures", {})))
    readings.extend(_thermal_glances_readings(payloads.get("hosts.temperatures", {})))
    readings.extend(_thermal_frigate_readings(payloads.get("frigate.stats", {})))
    return [_json_safe(item) for item in readings if item.get("temperature_c") is not None]


def _thermal_ups_readings(payload: Any) -> list[dict[str, Any]]:
    raw_ups = payload.get("ups") if isinstance(payload, dict) else None
    ups = raw_ups if isinstance(raw_ups, dict) else {}
    temperature = _as_float(ups.get("ups_temperature_c"))
    if temperature is None:
        return []
    name = str(ups.get("name") or "ups").strip()
    return [{
        "source": "nutups.status",
        "provider_id": "nutups",
        "sensor_id": f"nutups:{name}:ups",
        "label": f"UPS {name}",
        "category": "ups",
        "temperature_c": temperature,
        "status": ups.get("status"),
        "load_percent": ups.get("load_percent"),
        "battery_charge_percent": ups.get("battery_charge_percent"),
        "battery_runtime_seconds": ups.get("battery_runtime_seconds"),
    }]


def _thermal_opnsense_readings(payload: Any) -> list[dict[str, Any]]:
    raw_temperature = payload.get("temperature") if isinstance(payload, dict) else None
    temperature = raw_temperature if isinstance(raw_temperature, dict) else {}
    raw_sensors = temperature.get("sensors")
    sensors = raw_sensors if isinstance(raw_sensors, list) else []
    readings = []
    for sensor in sensors:
        if not isinstance(sensor, dict):
            continue
        sensor_id = str(sensor.get("sensor_id") or "sensor").strip()
        value = _as_float(sensor.get("temperature_c"))
        if value is None:
            continue
        readings.append({
            "source": "opnsense.system.temperature",
            "provider_id": "opnsense",
            "sensor_id": f"opnsense:{sensor_id}",
            "label": f"OPNsense {sensor_id}",
            "category": "opnsense",
            "kind": sensor.get("kind"),
            "temperature_c": value,
        })
    return readings


def _thermal_mikrotik_readings(payload: Any) -> list[dict[str, Any]]:
    raw_health = payload.get("health") if isinstance(payload, dict) else None
    health = raw_health if isinstance(raw_health, dict) else {}
    raw_sensors = health.get("temperature_sensors")
    sensors = raw_sensors if isinstance(raw_sensors, list) else []
    readings = []
    for sensor in sensors:
        if not isinstance(sensor, dict):
            continue
        sensor_id = str(sensor.get("sensor_id") or "sensor").strip()
        value = _as_float(sensor.get("temperature_c"))
        if value is None:
            continue
        readings.append({
            "source": "mikrotik.system.health",
            "provider_id": "mikrotik",
            "sensor_id": f"mikrotik:{sensor_id}",
            "label": f"MikroTik {sensor_id}",
            "category": "mikrotik",
            "kind": sensor.get("kind"),
            "temperature_c": value,
            "voltage_v": health.get("voltage_v"),
        })
    return readings


def _thermal_fritzbox_readings(provider_id: str, payload: Any) -> list[dict[str, Any]]:
    raw_temperature = payload.get("temperature") if isinstance(payload, dict) else None
    temperature = raw_temperature if isinstance(raw_temperature, dict) else {}
    if temperature.get("supported") is False:
        return []
    raw_sensors = temperature.get("sensors")
    sensors = raw_sensors if isinstance(raw_sensors, list) else []
    readings = []
    for sensor in sensors:
        if not isinstance(sensor, dict):
            continue
        sensor_id = str(sensor.get("sensor_id") or "cpu").strip()
        value = _as_float(sensor.get("temperature_c"))
        if value is None:
            continue
        readings.append({
            "source": f"{provider_id.replace('_', '.')}.temperature",
            "provider_id": provider_id,
            "sensor_id": f"{provider_id}:{sensor_id}",
            "label": f"{WATCHER_LABELS.get(provider_id, provider_id)} {sensor_id}",
            "category": "fritzbox",
            "kind": sensor.get("kind"),
            "temperature_c": value,
        })
    return readings


def _thermal_proxmox_disk_readings(payload: Any) -> list[dict[str, Any]]:
    raw_disks = payload.get("disks") if isinstance(payload, dict) else None
    disks = raw_disks if isinstance(raw_disks, list) else []
    readings = []
    for disk in disks:
        if not isinstance(disk, dict):
            continue
        node = str(disk.get("node") or "proxmox").strip()
        devpath = str(disk.get("devpath") or disk.get("disk_model") or "disk").strip()
        value = _as_float(disk.get("temperature_c"))
        if value is None:
            continue
        readings.append({
            "source": "proxmox.disks.temperatures",
            "provider_id": "proxmox",
            "sensor_id": f"proxmox:{node}:disk:{devpath}",
            "label": f"Proxmox {node} disk {disk.get('disk_model') or devpath}",
            "category": "disk",
            "node": node,
            "devpath": devpath,
            "disk_model": disk.get("disk_model"),
            "disk_type": disk.get("disk_type"),
            "temperature_c": value,
        })
    return readings


def _thermal_glances_readings(payload: Any) -> list[dict[str, Any]]:
    raw_hosts = payload.get("hosts") if isinstance(payload, dict) else None
    hosts = raw_hosts if isinstance(raw_hosts, list) else []
    readings = []
    for host in hosts:
        if not isinstance(host, dict) or host.get("error"):
            continue
        host_id = str(host.get("host_id") or "host").strip()
        category = "edge" if host_id in {"pizero-one", "pizero1", "qdevice"} else "compute"
        raw_sensors = host.get("sensors")
        sensors = raw_sensors if isinstance(raw_sensors, list) else []
        for sensor in sensors:
            if not isinstance(sensor, dict):
                continue
            sensor_id = str(sensor.get("sensor_id") or "sensor").strip()
            value = _as_float(sensor.get("temperature_c"))
            if value is None:
                continue
            sensor_kind = str(sensor.get("kind") or "").strip()
            readings.append({
                "source": "hosts.temperatures",
                "provider_id": "glances",
                "sensor_id": f"glances:{host_id}:{sensor_id}",
                "label": f"{host_id} {sensor_id}",
                "category": "disk" if sensor_kind == "disk" else category,
                "kind": sensor_kind,
                "host_id": host_id,
                "temperature_c": value,
            })
    return readings


def _thermal_frigate_readings(payload: Any) -> list[dict[str, Any]]:
    raw_service = payload.get("service") if isinstance(payload, dict) else None
    service = raw_service if isinstance(raw_service, dict) else {}
    raw_temperatures = service.get("temperatures")
    temperatures = raw_temperatures if isinstance(raw_temperatures, dict) else {}
    readings = []
    for name, raw_value in temperatures.items():
        value = _as_float(raw_value)
        if value is None:
            continue
        sensor_id = str(name or "detector").strip()
        readings.append({
            "source": "frigate.stats",
            "provider_id": "frigate",
            "sensor_id": f"frigate:{sensor_id}",
            "label": f"Frigate {sensor_id}",
            "category": "frigate",
            "kind": "detector",
            "temperature_c": value,
        })
    return readings


def _thermal_incidents(readings: list[dict[str, Any]]) -> list[DetectedIncident]:
    incidents: list[DetectedIncident] = []
    for reading in readings:
        category = str(reading.get("category") or "compute")
        thresholds = THERMAL_THRESHOLDS.get(category, THERMAL_THRESHOLDS["compute"])
        value = _as_float(reading.get("temperature_c"))
        if value is None:
            continue
        severity = None
        threshold = None
        if value >= thresholds["critical"]:
            severity = "critical"
            threshold = thresholds["critical"]
        elif value >= thresholds["warning"]:
            severity = "warning"
            threshold = thresholds["warning"]
        if severity is None or threshold is None:
            continue

        sensor_id = str(reading.get("sensor_id") or "sensor").strip()
        label = str(reading.get("label") or sensor_id).strip()
        provider_id = str(reading.get("provider_id") or "thermal").strip()
        message = f"{label} temperature is {value:g} C (threshold {threshold:g} C)"
        incidents.append(
            DetectedIncident(
                watcher_id="thermal.sensors",
                dedupe_key=_dedupe_key("thermal.sensors", provider_id, sensor_id),
                dedupe_basis=sensor_id,
                severity=severity,
                provider_id=provider_id,
                title=f"[Thermal Watcher] {message}"[:256],
                description=message[:4000],
                payload=_json_safe(redact({
                    "code": "temperature_threshold",
                    "sensor": reading,
                    "threshold_c": threshold,
                    "thresholds": thresholds,
                })),
            )
        )
    return incidents


def _backup_freshness_incidents(
    proxmox_payload: dict[str, Any], pbs_payload: dict[str, Any]
) -> list[DetectedIncident]:
    incidents: list[DetectedIncident] = []
    pbs_guest_ids = {
        int(group["backup_id"])
        for group in pbs_payload.get("backup_groups", [])
        if isinstance(group, dict)
        and str(group.get("backup_type") or "").lower() in {"vm", "ct"}
        and str(group.get("backup_id") or "").isdigit()
    }
    incidents.extend(
        _proxmox_backup_incidents(
            proxmox_payload,
            additional_seen_vmids=pbs_guest_ids,
        )
    )
    incidents.extend(_pbs_backup_incidents(pbs_payload))
    return incidents


def _proxmox_backup_incidents(
    payload: dict[str, Any], *, additional_seen_vmids: set[int] | None = None
) -> list[DetectedIncident]:
    raw_guests = payload.get("backups_by_guest")
    guests = raw_guests if isinstance(raw_guests, list) else []
    if not guests and not payload and not additional_seen_vmids:
        return []
    config = inventory.provider_config("proxmox")
    max_age = _as_float(config.get("backup_max_age_days")) or BACKUP_DEFAULT_MAX_AGE_DAYS
    ignored_vmids = {int(item) for item in config.get("backup_ignore_vmids", []) if str(item).isdigit()}
    required_vmids = {int(item) for item in config.get("required_backup_vmids", []) if str(item).isdigit()}

    incidents: list[DetectedIncident] = []
    seen_vmids: set[int] = set(additional_seen_vmids or set())
    for guest in guests:
        if not isinstance(guest, dict):
            continue
        vmid = guest.get("vmid")
        if not isinstance(vmid, int) or vmid in ignored_vmids:
            continue
        seen_vmids.add(vmid)
        age = _as_float(guest.get("latest_age_days"))
        if age is None or age <= max_age:
            continue
        severity = "critical" if age > max_age * 2 else "warning"
        basis = f"backup_stale:{vmid}"
        message = (
            f"latest vzdump backup for guest {vmid} is {age:g} day(s) old "
            f"(threshold {max_age:g} day(s))"
        )
        incidents.append(
            DetectedIncident(
                watcher_id="backup.freshness",
                dedupe_key=_dedupe_key("backup.freshness", "proxmox", basis),
                dedupe_basis=basis,
                severity=severity,
                provider_id="proxmox",
                title=f"[Backup Watcher] {message}"[:256],
                description=message[:4000],
                payload=_json_safe(redact({
                    "code": "backup_stale",
                    "guest": guest,
                    "max_age_days": max_age,
                })),
            )
        )
    for vmid in sorted(required_vmids - seen_vmids - ignored_vmids):
        basis = f"backup_missing:{vmid}"
        message = f"required guest {vmid} has no visible vzdump backup"
        incidents.append(
            DetectedIncident(
                watcher_id="backup.freshness",
                dedupe_key=_dedupe_key("backup.freshness", "proxmox", basis),
                dedupe_basis=basis,
                severity="warning",
                provider_id="proxmox",
                title=f"[Backup Watcher] {message}"[:256],
                description=message[:4000],
                payload=_json_safe(redact({"code": "backup_missing", "vmid": vmid})),
            )
        )
    return incidents


def _pbs_backup_incidents(payload: dict[str, Any]) -> list[DetectedIncident]:
    groups = payload.get("backup_groups") if isinstance(payload.get("backup_groups"), list) else []
    if not groups:
        return []
    config = inventory.provider_config("pbs")
    max_age = _as_float(config.get("backup_group_max_age_days")) or BACKUP_DEFAULT_MAX_AGE_DAYS
    ignored_groups = {str(item).strip() for item in config.get("backup_ignore_groups", [])}
    now = datetime.now(UTC).timestamp()

    incidents: list[DetectedIncident] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        store = str(group.get("store") or "").strip()
        group_key = f"{group.get('backup_type')}/{group.get('backup_id')}"
        if group_key in ignored_groups or f"{store}:{group_key}" in ignored_groups:
            continue
        latest = group.get("latest_backup_at")
        if not isinstance(latest, (int, float)) or latest <= 0:
            continue
        age_days = (now - float(latest)) / 86400
        if age_days <= max_age:
            continue
        severity = "critical" if age_days > max_age * 2 else "warning"
        basis = f"pbs_backup_stale:{store}:{group_key}"
        message = (
            f"latest PBS snapshot for {group_key} on {store or 'datastore'} is "
            f"{age_days:.1f} day(s) old (threshold {max_age:g} day(s))"
        )
        incidents.append(
            DetectedIncident(
                watcher_id="backup.freshness",
                dedupe_key=_dedupe_key("backup.freshness", "pbs", basis),
                dedupe_basis=basis,
                severity=severity,
                provider_id="pbs",
                title=f"[Backup Watcher] {message}"[:256],
                description=message[:4000],
                payload=_json_safe(redact({
                    "code": "pbs_backup_stale",
                    "group": group,
                    "age_days": round(age_days, 1),
                    "max_age_days": max_age,
                })),
            )
        )
    return incidents


def _disk_health_incidents(payload: dict[str, Any]) -> list[DetectedIncident]:
    raw_disks = payload.get("disks")
    disks = raw_disks if isinstance(raw_disks, list) else []
    incidents: list[DetectedIncident] = []
    for disk in disks:
        if not isinstance(disk, dict):
            continue
        health = str(disk.get("health") or "").strip()
        if health.lower() in DISK_HEALTH_OK_VALUES:
            continue
        node = str(disk.get("node") or "node").strip()
        devpath = str(disk.get("devpath") or "disk").strip()
        model = str(disk.get("disk_model") or "").strip()
        basis = f"disk_health:{node}:{devpath}"
        label = f"{devpath} ({model})" if model else devpath
        message = f"disk {label} on {node} reports SMART health: {health}"
        incidents.append(
            DetectedIncident(
                watcher_id="storage.disks",
                dedupe_key=_dedupe_key("storage.disks", "proxmox", basis),
                dedupe_basis=basis,
                severity="critical",
                provider_id="proxmox",
                title=f"[Disk Watcher] {message}"[:256],
                description=message[:4000],
                payload=_json_safe(redact({"code": "disk_health", "disk": disk})),
            )
        )
    return incidents


def _certificate_incidents(payload: dict[str, Any]) -> list[DetectedIncident]:
    raw_certificates = payload.get("certificates")
    certificates = raw_certificates if isinstance(raw_certificates, list) else []
    incidents: list[DetectedIncident] = []
    for certificate in certificates:
        if not isinstance(certificate, dict):
            continue
        days = _as_float(certificate.get("days_until_expiry"))
        if days is None:
            # Unreachable targets stay visible in the run payload; availability
            # ownership belongs to Uptime Kuma, not this watcher.
            continue
        warning_days_value = _as_float(certificate.get("warning_days"))
        critical_days_value = _as_float(certificate.get("critical_days"))
        warning_days = 21.0 if warning_days_value is None else warning_days_value
        critical_days = 7.0 if critical_days_value is None else critical_days_value
        target_id = str(certificate.get("id") or "target").strip()
        name = str(certificate.get("name") or target_id).strip()
        if days <= 0:
            severity = "critical"
            message = f"TLS certificate for {name} expired {abs(days):.1f} day(s) ago"
        elif days <= critical_days:
            severity = "critical"
            message = f"TLS certificate for {name} expires in {days:.1f} day(s)"
        elif days <= warning_days:
            severity = "warning"
            message = f"TLS certificate for {name} expires in {days:.1f} day(s)"
        else:
            continue
        basis = f"cert_expiry:{target_id}"
        incidents.append(
            DetectedIncident(
                watcher_id="security.certificates",
                dedupe_key=_dedupe_key("security.certificates", "network", basis),
                dedupe_basis=basis,
                severity=severity,
                provider_id="network",
                title=f"[Certificate Watcher] {message}"[:256],
                description=message[:4000],
                payload=_json_safe(redact({"code": "cert_expiry", "certificate": certificate})),
            )
        )
    return incidents


def _normalize_mac(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", ":")
    if not raw:
        return ""
    parts = raw.split(":")
    if len(parts) == 6 and all(parts):
        return ":".join(part.zfill(2) for part in parts)
    compact = re.sub(r"[^0-9a-f]", "", raw)
    if len(compact) == 12:
        return ":".join(compact[index:index + 2] for index in range(0, 12, 2))
    return raw


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:[.,]\d+)?", value)
        if match:
            return float(match.group(0).replace(",", "."))
    return None


def _device_label(device: dict[str, Any]) -> str:
    hostnames = device.get("hostnames") if isinstance(device.get("hostnames"), list) else []
    ips = device.get("ips") if isinstance(device.get("ips"), list) else []
    hostname = hostnames[0] if hostnames else "unknown-host"
    ip = ips[0] if ips else "unknown-ip"
    mac = device.get("mac") or "unknown-mac"
    return f"{hostname} ({ip}, {mac})"


def _refresh_incident(incident: Incident, detected: DetectedIncident, *, now) -> None:
    incident.dedupe_key = detected.dedupe_key
    incident.last_seen_at = now
    incident.occurrences += 1
    incident.severity = detected.severity
    incident.title = detected.title
    incident.description = detected.description
    incident.payload = _incident_payload(detected, "updated_existing_task")
    incident.missing_runs = 0
    incident.last_missing_at = None


def _incident_from_finding(finding: dict[str, Any]) -> DetectedIncident:
    provider_id = str(finding.get("provider_id") or "lab")
    severity = _severity(str(finding.get("severity") or "warning"))
    message = str(finding.get("message") or finding.get("title") or "Homelab alert").strip()
    basis = _finding_dedupe_basis(finding, message)
    title = f"[Watcher] {provider_id}: {message}"[:256]
    description = message[:4000]
    payload = _json_safe(redact(finding))
    return DetectedIncident(
        watcher_id="lab.alerts",
        dedupe_key=_dedupe_key("lab.alerts", provider_id, basis),
        dedupe_basis=basis,
        severity=severity,
        provider_id=provider_id,
        title=title,
        description=description,
        payload=payload,
    )


def _is_actionable(finding: Any, watcher_id: str) -> bool:
    if not _is_observable_finding(finding):
        return False
    raw_severity = str(finding.get("severity") or "").lower()
    severity = _severity(raw_severity)
    if SEVERITY_RANK.get(severity, 0) < SEVERITY_RANK[_watcher_config(watcher_id).min_severity]:
        return False
    haystack = " ".join(
        str(finding.get(key) or "").lower()
        for key in ("provider_id", "severity", "message", "title", "description")
    )
    return not any(pattern in haystack for pattern in get_settings().watchers_ignore_pattern_list)


def _is_observable_finding(finding: Any) -> bool:
    return (
        isinstance(finding, dict)
        and str(finding.get("severity") or "").lower() in {"critical", "warning"}
    )


def _incident_filter_reason(watcher_id: str, incident: DetectedIncident) -> str:
    minimum = _watcher_config(watcher_id).min_severity
    if SEVERITY_RANK.get(incident.severity, 0) < SEVERITY_RANK[minimum]:
        return "below_min_severity"
    haystack = " ".join(
        (incident.provider_id, incident.severity, incident.title, incident.description)
    ).lower()
    if any(pattern in haystack for pattern in get_settings().watchers_ignore_pattern_list):
        return "ignored_pattern"
    return "policy_filtered"


def _severity(value: str) -> str:
    value = value.lower()
    return value if value in {"critical", "warning", "info"} else "warning"


def _minimum_severity() -> str:
    configured = get_settings().watchers_min_severity.lower()
    return configured if configured in {"warning", "critical"} else "warning"


def _resolve_after_missing_runs() -> int:
    return max(1, get_settings().watchers_resolve_after_missing_runs)


def _incident_payload(detected: DetectedIncident, action: str) -> dict[str, Any]:
    payload = detected.payload if isinstance(detected.payload, dict) else {}
    merged = {
        **payload,
        "dedupe_basis": detected.dedupe_basis,
        "dedupe_action": action,
        "policy_state": "actionable",
        "auto_close_policy": {
            "resolve_after_missing_runs": _resolve_after_missing_runs(),
            "auto_complete_only_if_unclaimed": True,
        },
        "runbook_incident_type": WATCHER_RUNBOOKS.get(detected.watcher_id),
    }
    return _json_safe(redact(merged))


def _incident_dedupe_note(incident: Incident) -> str:
    if incident.occurrences <= 1:
        return "Created a new watcher incident/task."
    return f"Updated existing watcher incident {incident.occurrences} time(s)."


def _incident_auto_close_note(incident: Incident) -> str:
    if incident.status == "resolved" and incident.resolution_reason == "alert_cleared":
        return "Alert cleared after the grace period; unclaimed watcher tasks can be auto-completed."
    return (
        f"Auto-close after {_resolve_after_missing_runs()} missed run(s), only if the linked task "
        "is still unclaimed."
    )


def _incident_runbook_type(incident: Incident) -> str | None:
    if isinstance(incident.payload, dict) and incident.payload.get("runbook_incident_type"):
        return str(incident.payload["runbook_incident_type"])
    return WATCHER_RUNBOOKS.get(incident.watcher_id)


def _dedupe_key(watcher_id: str, provider_id: str, message: str) -> str:
    raw = f"{watcher_id}|{provider_id}|{message.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _finding_dedupe_basis(finding: dict[str, Any], message: str) -> str:
    for key in ("dedupe_key", "fingerprint", "id", "code"):
        value = str(finding.get(key) or "").strip()
        if value:
            return value.lower()
    return _dedupe_basis(message)


def _dedupe_basis(message: str) -> str:
    normalized = message.strip().lower()
    normalized = re.sub(r"\b\d+\b", "#", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _task_goal(incident: DetectedIncident) -> str:
    return (
        f"Watcher `{incident.watcher_id}` detected a {incident.severity} alert "
        f"on `{incident.provider_id}`.\n\n"
        f"Detail: {incident.description}\n\n"
        "Check the status with read-only summary tools and update this task with "
        "cause, impact, and next action."
    )[:4000]


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


async def _try_acquire_watcher_lock(db: AsyncSession, watcher_id: str) -> bool:
    return await try_advisory_xact_lock(db, f"homelab.watcher.{watcher_id}")


async def watcher_scheduler_loop() -> None:
    settings = get_settings()
    interval = max(60, settings.watchers_interval_seconds)
    await asyncio.sleep(5)
    while True:
        try:
            async with get_session_factory()() as db:
                if (
                    await try_advisory_xact_lock(db, "homelab.watcher.scheduler")
                    and await _load_watcher_automation_enabled(db)
                ):
                    due = await _due_watcher_ids(db)
                    if due:
                        await run_watchers(db, watcher_ids=due)
                    await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("watcher scheduler run failed")
        await asyncio.sleep(min(10, interval))


async def _due_watcher_ids(db: AsyncSession) -> set[str]:
    await _load_watcher_configs(db)
    latest_by_id: dict[str, WatcherRun] = {}
    rows = (
        await db.execute(select(WatcherRun).order_by(WatcherRun.started_at.desc()).limit(100))
    ).scalars()
    for run in rows:
        latest_by_id.setdefault(run.watcher_id, run)

    now = utcnow()
    due: set[str] = set()
    for watcher_id in _enabled_watcher_ids():
        config = _watcher_config(watcher_id)
        latest = latest_by_id.get(watcher_id)
        if latest is None:
            due.add(watcher_id)
            continue
        if _aware_utc(latest.started_at) + timedelta(seconds=config.interval_seconds) <= now:
            due.add(watcher_id)
    return due


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
