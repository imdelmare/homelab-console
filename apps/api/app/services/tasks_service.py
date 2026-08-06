from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Finding, Incident, McpClient, Task, TaskCheck, TaskEvent, TaskResolution, ToolInvocation, utcnow
from app.domain.actors import Actor
from app.services import task_notifications
from app.services.audit import write_audit
from app.services.redaction import redact

TASK_STATUSES = {
    "open",
    "claimed",
    "investigating",
    "waiting_operator",
    "blocked",
    "completed",
    "cancelled",
}
FINAL_TASK_STATUSES = {"completed", "cancelled"}
TASK_TRANSITIONS = {
    "open": {"claimed", "cancelled"},
    "claimed": {"investigating", "open", "cancelled"},
    "investigating": {"waiting_operator", "blocked", "completed", "cancelled"},
    "waiting_operator": {"investigating", "cancelled"},
    "blocked": {"investigating", "cancelled"},
    "completed": {"open"},
    "cancelled": {"open"},
}
FINDING_SEVERITIES = {"info", "warning", "critical"}
CHECK_STATUSES = {"pending", "completed", "skipped"}
VALID_AGENT_NAMES = {"claude", "fixer", "codex", "cline", "opencode"}
VALID_AGENT_IDS = {f"agent:{agent}" for agent in VALID_AGENT_NAMES}
RELEASABLE_STATUSES = {"claimed", "investigating", "waiting_operator", "blocked"}
MAX_LIST_LIMIT = 100
TRANSITION_POLICY_NORMAL = "normal"
TRANSITION_POLICY_RELEASE = "release"
TRANSITION_POLICY_WATCHER_AUTO_CLEARED = "watcher_auto_cleared"
TRANSITION_POLICY_OPERATOR_HANDLED = "operator_handled"


@dataclass
class TaskServiceError(Exception):
    code: str
    message: str


def _actor_id(actor: Actor) -> str:
    if actor.kind == "service" and actor.id in VALID_AGENT_NAMES:
        return f"agent:{actor.id}"
    return actor.audit_id()


def agent_identity(actor: Actor) -> str | None:
    """Canonical ``agent:<id>`` if the actor is an MCP agent (kind "agent", or
    the "service"/agent-id form used by some callers/tests),
    None if the actor is an operator (user/telegram) who gets administrative
    override. An "agent" actor with an id outside the configured agents never matches
    any assignment, so it is always rejected by ownership checks."""
    if actor.kind in {"agent", "service"} and actor.id in VALID_AGENT_NAMES:
        return f"agent:{actor.id}"
    if actor.kind == "agent":
        return f"agent:{actor.id}"
    return None


def _require_ownership(task: Task, actor: Actor) -> None:
    agent = agent_identity(actor)
    if agent is None:
        return
    if task.assigned_agent != agent:
        raise TaskServiceError(
            "not_task_owner",
            "task is not assigned to this agent; claim it first"
            if not task.assigned_agent
            else "task is assigned to another agent",
        )


def _require_active(task: Task, action: str) -> None:
    if task.status in FINAL_TASK_STATUSES:
        raise TaskServiceError(
            "task_not_active",
            f"task is {task.status}; reopen it before {action}",
        )


def _activity(task: Task) -> None:
    now = utcnow()
    task.last_activity_at = now
    task.updated_at = now
    task.version += 1


def _expect_version(task: Task, expected_version: int | None) -> None:
    if expected_version is not None and task.version != expected_version:
        raise TaskServiceError("version_conflict", "task version does not match")


def _validate_text(value: str, field: str, *, max_length: int, required: bool = False) -> str:
    value = value.strip()
    if required and not value:
        raise TaskServiceError("invalid_input", f"{field} is required")
    if len(value) > max_length:
        raise TaskServiceError("invalid_input", f"{field} is too long")
    return value


async def _event(
    db: AsyncSession,
    task: Task,
    kind: str,
    payload: dict,
    actor: Actor,
    source: str,
    *,
    audit_action: str | None = None,
) -> TaskEvent:
    safe_payload = redact(payload)
    event = TaskEvent(task_id=task.id, kind=kind, payload=safe_payload)
    db.add(event)
    await write_audit(
        db,
        actor=actor,
        source=source,
        action=audit_action or kind,
        outcome="success",
        task_id=task.id,
        metadata=safe_payload,
    )
    return event


async def _locked_task(db: AsyncSession, task_id: str) -> Task:
    result = await db.execute(select(Task).where(Task.id == task_id).with_for_update())
    task = result.scalar_one_or_none()
    if task is None:
        raise TaskServiceError("unknown_task", "unknown task")
    return task


async def create_task(
    db: AsyncSession,
    title: str,
    goal: str,
    actor: Actor,
    source: str = "rest",
    *,
    notify: bool = True,
) -> Task:
    title = _validate_text(title, "title", max_length=256, required=True)
    goal = _validate_text(goal, "goal", max_length=4000)

    now = utcnow()
    task = Task(
        title=title,
        goal=goal,
        source=source,
        created_by=_actor_id(actor),
        last_activity_at=now,
    )
    db.add(task)
    await db.flush()
    await _event(db, task, "task.created", {"title": title}, actor, source)
    return task


async def create_provider_task(
    db: AsyncSession,
    title: str,
    goal: str,
    actor: Actor,
    *,
    provider_context: dict,
) -> Task:
    task = await create_task(db, title, goal, actor, source="provider")
    await _event(db, task, "task.provider_context", provider_context, actor, "provider")
    return task


async def list_tasks(
    db: AsyncSession,
    *,
    status: str | None = None,
    assigned_agent: str | None = None,
    limit: int = MAX_LIST_LIMIT,
) -> list[Task]:
    if status is not None and status not in TASK_STATUSES:
        raise TaskServiceError("invalid_input", "invalid task status")
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    query = select(Task)
    if status is not None:
        query = query.where(Task.status == status)
    if assigned_agent is not None:
        query = query.where(Task.assigned_agent == assigned_agent)
    result = await db.execute(query.order_by(Task.last_activity_at.desc()).limit(limit))
    return list(result.scalars())


async def task_router_statuses(db: AsyncSession, task_ids: list[str]) -> dict[str, str]:
    if not task_ids:
        return {}
    from app.db.models import TaskRouterJob

    job_rows = (
        await db.execute(
            select(TaskRouterJob.task_id, TaskRouterJob.status).where(
                TaskRouterJob.task_id.in_(task_ids)
            )
        )
    ).all()
    status_map = {
        "pending": "queued",
        "running": "running",
        "succeeded": "routed",
        "policy_failed": "policy_failed",
        "failed": "failed",
    }
    statuses = {task_id: status_map.get(status, status) for task_id, status in job_rows}
    rows = (
        await db.execute(
            select(TaskEvent.task_id, TaskEvent.kind)
            .where(
                TaskEvent.task_id.in_(task_ids),
                TaskEvent.kind.in_(("task.router_decision", "task.router_failed")),
            )
            .order_by(TaskEvent.created_at.desc())
        )
    ).all()
    for task_id, kind in rows:
        if statuses.get(task_id) in {"routed", "policy_failed", "failed"}:
            continue
        statuses[task_id] = "failed" if kind == "task.router_failed" else "routed"
    return statuses


async def task_resolution_labels(db: AsyncSession, task_ids: list[str]) -> dict[str, str]:
    if not task_ids:
        return {}
    rows = (
        await db.execute(
            select(TaskEvent.task_id, TaskEvent.kind)
            .where(
                TaskEvent.task_id.in_(task_ids),
                TaskEvent.kind.in_(
                    (
                        "watcher.task.auto_completed",
                        "watcher.incident.resolve_handled",
                        "watcher.incident.auto_matched",
                        "task.operator_completed",
                    )
                ),
            )
            .order_by(TaskEvent.created_at.desc())
        )
    ).all()
    labels: dict[str, str] = {}
    for task_id, kind in rows:
        if task_id in labels:
            continue
        if kind == "watcher.task.auto_completed":
            labels[task_id] = "auto_closed"
        elif kind == "watcher.incident.resolve_handled":
            labels[task_id] = "operator_handled"
        elif kind == "watcher.incident.auto_matched":
            labels[task_id] = "already_handled"
        elif kind == "task.operator_completed":
            labels[task_id] = "human_handled"
    return labels


async def get_task(db: AsyncSession, task_id: str) -> Task | None:
    return await db.get(Task, task_id)


async def record_fixer_dispatch_requested(
    db: AsyncSession,
    task: Task,
    actor: Actor,
    *,
    source: str,
) -> TaskEvent:
    event = await _event(
        db,
        task,
        "task.fixer_dispatch_requested",
        {
            "assigned_agent": task.assigned_agent,
            "authorized_by": actor.audit_id(),
            "dispatch_kind": "operator" if actor.kind in {"user", "telegram"} else "policy",
        },
        actor,
        source,
    )
    await db.flush()
    return event


async def transition_task(
    db: AsyncSession,
    task: Task,
    status: str,
    actor: Actor,
    *,
    source: str = "rest",
    policy: str = TRANSITION_POLICY_NORMAL,
    reason: str = "",
    incident_id: str | None = None,
    details: dict | None = None,
    notify: bool = True,
) -> Task:
    """Apply a task status transition through one auditable state machine.

    Watcher completion is an explicit, guarded policy exception rather than
    an implicit addition to the normal public transition graph.
    """
    if status not in TASK_STATUSES:
        raise TaskServiceError("invalid_status", "invalid task status")
    previous = task.status
    if policy == TRANSITION_POLICY_NORMAL:
        if status not in TASK_TRANSITIONS[previous]:
            raise TaskServiceError("invalid_transition", f"cannot transition {previous} to {status}")
    elif policy == TRANSITION_POLICY_WATCHER_AUTO_CLEARED:
        if (
            status != "completed"
            or previous != "open"
            or task.source != "watcher"
            or task.assigned_agent
            or task.claimed_at is not None
        ):
            raise TaskServiceError("invalid_transition", "task is not eligible for watcher auto-completion")
    elif policy == TRANSITION_POLICY_OPERATOR_HANDLED:
        if status != "completed" or previous in FINAL_TASK_STATUSES:
            raise TaskServiceError("invalid_transition", "task is not eligible for handled completion")
    elif policy == TRANSITION_POLICY_RELEASE:
        if status != "open" or previous not in RELEASABLE_STATUSES:
            raise TaskServiceError("invalid_transition", "task is not eligible for release")
    else:
        raise TaskServiceError("invalid_input", "unknown transition policy")

    task.status = status
    reopened = previous in FINAL_TASK_STATUSES and status == "open"
    if status == "completed":
        task.completed_at = utcnow()
        if policy != TRANSITION_POLICY_NORMAL:
            task.assigned_agent = ""
            task.claimed_at = None
        await capture_task_resolution(db, task, actor, source=source)
    elif reopened:
        task.completed_at = None
    if status == "open":
        task.assigned_agent = ""
        task.claimed_at = None
    _activity(task)

    payload = {
        "from": previous,
        "to": status,
        "reason": reason,
        "automatic": policy == TRANSITION_POLICY_WATCHER_AUTO_CLEARED,
        "policy": policy,
    }
    if incident_id:
        payload["incident_id"] = incident_id
    if details:
        payload.update(details)
    await _event(db, task, "task.status_changed", payload, actor, source)
    if reopened:
        await _event(db, task, "task.reopened", {"from_status": previous}, actor, source)
    elif notify:
        await task_notifications.notify_status_change(
            db, task.title, previous, status, task_id=task.id
        )
    return task


async def set_status(
    db: AsyncSession,
    task_id: str,
    status: str,
    actor: Actor,
    *,
    expected_version: int | None = None,
    source: str = "rest",
) -> Task:
    task = await _locked_task(db, task_id)
    _expect_version(task, expected_version)
    _require_ownership(task, actor)
    return await transition_task(db, task, status, actor, source=source)


async def claim_task(
    db: AsyncSession,
    task_id: str,
    agent_id: str,
    actor: Actor,
    *,
    source: str = "mcp",
) -> Task:
    agent_id = _validate_text(agent_id, "agent_id", max_length=80, required=True)
    if agent_id not in VALID_AGENT_IDS:
        raise TaskServiceError(
            "invalid_input", f"agent_id must be one of: {', '.join(sorted(VALID_AGENT_IDS))}"
        )
    task = await _locked_task(db, task_id)
    if task.status == "claimed" and task.assigned_agent == agent_id:
        return task
    if task.status == "claimed" and task.assigned_agent and task.assigned_agent != agent_id:
        raise TaskServiceError("task_already_claimed", "task is already claimed")
    if task.status != "open":
        raise TaskServiceError("invalid_transition", "only open tasks can be claimed")
    if task.assigned_agent and task.assigned_agent != agent_id:
        raise TaskServiceError("task_already_claimed", "task is already claimed")

    task.assigned_agent = agent_id
    task.claimed_at = utcnow()
    await transition_task(
        db,
        task,
        "claimed",
        actor,
        source=source,
        reason="task_claimed",
        details={"assigned_agent": agent_id},
        notify=False,
    )
    await _event(db, task, "task.claimed", {"assigned_agent": agent_id}, actor, source)
    return task


async def claim_task_as_operator(
    db: AsyncSession,
    task_id: str,
    actor: Actor,
    *,
    expected_version: int | None = None,
    source: str = "rest",
) -> Task:
    """Assign an open task to the authenticated human operator.

    The owner is derived from the authenticated actor rather than accepted
    from request input, so a browser cannot claim a task as another user.
    """
    if actor.kind != "user":
        raise TaskServiceError("unauthorized", "operator claim requires a user session")
    owner = actor.audit_id()
    task = await _locked_task(db, task_id)
    _expect_version(task, expected_version)
    if task.status == "claimed" and task.assigned_agent == owner:
        return task
    if task.status != "open" or task.assigned_agent:
        raise TaskServiceError("task_already_claimed", "task is not available for operator claim")

    task.assigned_agent = owner
    task.claimed_at = utcnow()
    await transition_task(
        db,
        task,
        "claimed",
        actor,
        source=source,
        reason="task_claimed",
        details={"assigned_agent": owner},
        notify=False,
    )
    await _event(db, task, "task.claimed", {"assigned_agent": owner}, actor, source)
    return task


def _require_operator_owner(task: Task, actor: Actor) -> str:
    if actor.kind != "user":
        raise TaskServiceError("unauthorized", "operator action requires a user session")
    owner = actor.audit_id()
    if task.assigned_agent != owner:
        raise TaskServiceError("not_operator_owner", "take ownership of the task before this action")
    return owner


def _is_recent_mcp_client(client: McpClient) -> bool:
    if client.revoked_at is not None or client.last_seen_at is None:
        return False
    last_seen = client.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    return datetime.now(UTC) - last_seen < timedelta(seconds=120)


async def handoff_operator_task_to_client(
    db: AsyncSession,
    task_id: str,
    client_id: str,
    note: str,
    actor: Actor,
    *,
    expected_version: int | None = None,
    source: str = "rest",
) -> Task:
    """Atomically record an operator note and hand the task to an online MCP agent."""
    note = _validate_text(note, "note", max_length=4000, required=True)
    task = await _locked_task(db, task_id)
    _expect_version(task, expected_version)
    operator_owner = _require_operator_owner(task, actor)
    if task.status not in RELEASABLE_STATUSES:
        raise TaskServiceError("invalid_transition", "only active owned tasks can be handed off")

    client = await db.get(McpClient, client_id)
    if client is None:
        raise TaskServiceError("unknown_client", "unknown MCP client")
    if not _is_recent_mcp_client(client):
        raise TaskServiceError("client_offline", "MCP client is not currently online")
    target_owner = f"agent:{client.agent_id}"
    if target_owner not in VALID_AGENT_IDS:
        raise TaskServiceError("invalid_input", "MCP client agent cannot own tasks")

    await _event(db, task, "task.note_added", {"note": note}, actor, source)
    await transition_task(
        db,
        task,
        "open",
        actor,
        source=source,
        policy=TRANSITION_POLICY_RELEASE,
        reason="operator_handoff",
        details={"from_agent": operator_owner, "handoff_summary": note},
        notify=False,
    )
    task.assigned_agent = target_owner
    task.claimed_at = utcnow()
    await transition_task(
        db,
        task,
        "claimed",
        actor,
        source=source,
        reason="operator_handoff",
        details={"assigned_agent": target_owner},
        notify=False,
    )
    await _event(
        db,
        task,
        "task.operator_handoff",
        {
            "from_user": operator_owner,
            "to_agent": target_owner,
            "client_id": client.id,
            "client_label": client.client_label,
            "note": note,
        },
        actor,
        source,
    )
    return task


async def complete_task_as_operator(
    db: AsyncSession,
    task_id: str,
    note: str,
    actor: Actor,
    *,
    expected_version: int | None = None,
    source: str = "rest",
) -> Task:
    """Record the human resolution note and complete an operator-owned task."""
    note = _validate_text(note, "note", max_length=4000, required=True)
    task = await _locked_task(db, task_id)
    _expect_version(task, expected_version)
    owner = _require_operator_owner(task, actor)
    if task.status not in RELEASABLE_STATUSES:
        raise TaskServiceError("invalid_transition", "only active owned tasks can be completed")

    await _event(db, task, "task.note_added", {"note": note}, actor, source)
    await transition_task(
        db,
        task,
        "completed",
        actor,
        source=source,
        policy=TRANSITION_POLICY_OPERATOR_HANDLED,
        reason="human_handled",
        details={"handled_by": owner},
    )
    await _event(
        db,
        task,
        "task.operator_completed",
        {"handled_by": owner, "note": note},
        actor,
        source,
    )
    return task


async def release_task(
    db: AsyncSession,
    task_id: str,
    actor: Actor,
    *,
    expected_version: int | None = None,
    handoff_summary: str = "",
    source: str = "mcp",
) -> Task:
    handoff_summary = _validate_text(handoff_summary, "handoff_summary", max_length=4000)
    task = await _locked_task(db, task_id)
    _expect_version(task, expected_version)
    _require_ownership(task, actor)
    if task.status not in RELEASABLE_STATUSES:
        raise TaskServiceError("invalid_transition", "only active assigned tasks can be released")
    if agent_identity(actor) is not None and not handoff_summary:
        raise TaskServiceError("invalid_input", "handoff_summary is required when an agent releases a task")
    previous_status = task.status
    previous_agent = task.assigned_agent
    await transition_task(
        db,
        task,
        "open",
        actor,
        source=source,
        policy=TRANSITION_POLICY_RELEASE,
        reason="task_released",
        details={
            "from_agent": previous_agent,
            "handoff_summary": handoff_summary,
        },
        notify=False,
    )
    await _event(
        db,
        task,
        "task.released",
        {"from_status": previous_status, "from_agent": previous_agent, "handoff_summary": handoff_summary},
        actor,
        source,
    )
    return task


async def update_summary(
    db: AsyncSession,
    task_id: str,
    summary: str,
    actor: Actor,
    *,
    expected_version: int | None = None,
    source: str = "rest",
) -> Task:
    summary = _validate_text(summary, "summary", max_length=8000)
    task = await _locked_task(db, task_id)
    _require_active(task, "updating its summary")
    _expect_version(task, expected_version)
    _require_ownership(task, actor)
    task.summary = summary
    _activity(task)
    await _event(db, task, "task.summary_updated", {"summary": summary}, actor, source)
    return task


async def add_note(
    db: AsyncSession,
    task_id: str,
    note: str,
    actor: Actor,
    *,
    source: str = "rest",
) -> TaskEvent:
    note = _validate_text(note, "note", max_length=4000, required=True)
    task = await _locked_task(db, task_id)
    _require_active(task, "adding notes")
    _require_ownership(task, actor)
    _activity(task)
    safe_note = redact({"note": note})
    event = TaskEvent(task_id=task.id, kind="task.note_added", payload=safe_note)
    db.add(event)
    await write_audit(
        db,
        actor=actor,
        source=source,
        action="task.note_added",
        outcome="success",
        task_id=task.id,
        metadata=safe_note,
    )
    return event


async def add_finding(
    db: AsyncSession,
    task_id: str,
    severity: str,
    title: str,
    description: str,
    actor: Actor,
    *,
    source: str = "rest",
    tool_invocation_id: str | None = None,
) -> Finding:
    if severity not in FINDING_SEVERITIES:
        raise TaskServiceError("invalid_input", "invalid finding severity")
    title = _validate_text(title, "title", max_length=256, required=True)
    description = _validate_text(description, "description", max_length=4000, required=True)
    task = await _locked_task(db, task_id)
    _require_active(task, "adding findings")
    _require_ownership(task, actor)
    if tool_invocation_id:
        invocation = await db.get(ToolInvocation, tool_invocation_id)
        if invocation is None or invocation.task_id != task.id:
            raise TaskServiceError("invalid_input", "unknown tool invocation for task")

    finding = Finding(
        task_id=task.id,
        severity=severity,
        title=title,
        description=description,
        source=source,
        tool_invocation_id=tool_invocation_id,
        created_by=_actor_id(actor),
    )
    db.add(finding)
    _activity(task)
    await db.flush()
    await _event(
        db,
        task,
        "task.finding_added",
        {"finding_id": finding.id, "severity": severity, "title": title},
        actor,
        source,
    )
    if severity == "critical" and source != "watcher":
        await task_notifications.notify_critical_finding(
            db, task.title, title, task_id=task.id
        )
    return finding


async def resolve_finding(
    db: AsyncSession,
    task_id: str,
    finding_id: str,
    actor: Actor,
    *,
    source: str = "rest",
) -> Finding:
    task = await _locked_task(db, task_id)
    _require_active(task, "resolving findings")
    _require_ownership(task, actor)
    finding = await db.get(Finding, finding_id)
    if finding is None or finding.task_id != task.id:
        raise TaskServiceError("unknown_finding", "unknown finding")
    if finding.resolved_at is None:
        finding.resolved_at = utcnow()
        _activity(task)
        await _event(db, task, "task.finding_resolved", {"finding_id": finding.id}, actor, source)
    return finding


async def add_check(
    db: AsyncSession,
    task_id: str,
    description: str,
    actor: Actor,
    *,
    source: str = "rest",
) -> TaskCheck:
    description = _validate_text(description, "description", max_length=512, required=True)
    task = await _locked_task(db, task_id)
    _require_active(task, "adding checks")
    _require_ownership(task, actor)
    check = TaskCheck(task_id=task.id, description=description, created_by=_actor_id(actor))
    db.add(check)
    _activity(task)
    await db.flush()
    await _event(db, task, "task.check_added", {"check_id": check.id}, actor, source)
    return check


async def complete_check(
    db: AsyncSession,
    task_id: str,
    check_id: str,
    actor: Actor,
    *,
    source: str = "rest",
) -> TaskCheck:
    return await _finish_check(db, task_id, check_id, "completed", actor, source=source)


async def skip_check(
    db: AsyncSession,
    task_id: str,
    check_id: str,
    actor: Actor,
    reason: str,
    *,
    source: str = "rest",
) -> TaskCheck:
    reason = _validate_text(reason, "reason", max_length=1000, required=True)
    return await _finish_check(db, task_id, check_id, "skipped", actor, source=source, reason=reason)


async def _finish_check(
    db: AsyncSession,
    task_id: str,
    check_id: str,
    status: str,
    actor: Actor,
    *,
    source: str,
    reason: str = "",
) -> TaskCheck:
    task = await _locked_task(db, task_id)
    _require_active(task, "updating checks")
    _require_ownership(task, actor)
    check = await db.get(TaskCheck, check_id)
    if check is None or check.task_id != task.id:
        raise TaskServiceError("unknown_check", "unknown check")
    if check.status != "pending":
        raise TaskServiceError("invalid_transition", "check is already finished")
    check.status = status
    check.completed_by = _actor_id(actor)
    check.completed_at = utcnow()
    check.skip_reason = reason
    _activity(task)
    event_kind = "task.check_completed" if status == "completed" else "task.check_skipped"
    payload = {"check_id": check.id}
    if reason:
        payload["reason"] = reason
    await _event(db, task, event_kind, payload, actor, source)
    return check


async def complete_task(
    db: AsyncSession,
    task_id: str,
    actor: Actor,
    *,
    expected_version: int | None = None,
    source: str = "rest",
) -> Task:
    return await set_status(
        db,
        task_id,
        "completed",
        actor,
        expected_version=expected_version,
        source=source,
    )


async def capture_task_resolution(db: AsyncSession, task: Task, actor: Actor, *, source: str) -> None:
    """Append-only snapshot of task state at the moment it closes — seeds
    the future Knowledge Base (roadmap Phase 4). No incident_type/runbook
    step here (would require importing task_context.py/runbooks.py, a
    layering inversion); Phase 4 recomputes those later via incident_id.
    Called from set_status's "completed" branch, and from watchers.py's
    resolve_incident_as_handled path, so every real completion path is
    covered."""
    await db.flush()
    incident = (
        await db.execute(
            select(Incident)
            .where(Incident.task_id == task.id)
            .order_by(Incident.last_seen_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    findings = (
        await db.execute(
            select(Finding).where(Finding.task_id == task.id, Finding.resolved_at.is_not(None))
        )
    ).scalars()
    db.add(
        TaskResolution(
            task_id=task.id,
            incident_id=incident.id if incident else None,
            summary=task.summary,
            resolved_findings=[
                {"id": finding.id, "title": finding.title, "severity": finding.severity}
                for finding in findings
            ],
            resolved_by=actor.audit_id(),
            source=source,
        )
    )


async def list_resolutions(db: AsyncSession, task_id: str) -> list[TaskResolution]:
    return list(
        (
            await db.execute(
                select(TaskResolution)
                .where(TaskResolution.task_id == task_id)
                .order_by(TaskResolution.created_at.desc())
            )
        ).scalars()
    )


async def reopen_task(
    db: AsyncSession,
    task_id: str,
    actor: Actor,
    *,
    expected_version: int | None = None,
    source: str = "rest",
) -> Task:
    return await set_status(
        db,
        task_id,
        "open",
        actor,
        expected_version=expected_version,
        source=source,
    )


async def list_checks(
    db: AsyncSession,
    task_id: str,
    *,
    status: str | None = None,
) -> list[TaskCheck]:
    if status is not None and status not in CHECK_STATUSES:
        raise TaskServiceError("invalid_input", "invalid check status")
    query = select(TaskCheck).where(TaskCheck.task_id == task_id)
    if status is not None:
        query = query.where(TaskCheck.status == status)
    result = await db.execute(query.order_by(TaskCheck.created_at))
    return list(result.scalars())


async def list_events(db: AsyncSession, task_id: str, *, limit: int = 100) -> list[TaskEvent]:
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    result = await db.execute(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.created_at.desc())
        .limit(limit)
    )
    return list(reversed(list(result.scalars())))


async def list_findings(db: AsyncSession, task_id: str) -> list[Finding]:
    result = await db.execute(
        select(Finding).where(Finding.task_id == task_id).order_by(Finding.created_at)
    )
    return list(result.scalars())


def recommended_next_step(
    status: str, *, has_pending_checks: bool, has_open_critical_findings: bool
) -> str:
    """Deterministic handoff guidance, no LLM involved. Status-specific rules
    take precedence; within "investigating", pending checks are surfaced
    before open critical findings."""
    if status == "open":
        return "Claim the task before working on it."
    if status == "claimed":
        return "Set the task to investigating and begin with the relevant summary tool."
    if status == "waiting_operator":
        return "Wait for or request operator input."
    if status == "blocked":
        return "Document the blocker or release the task for handoff."
    if status == "completed":
        return "No further action is required unless the task is reopened."
    if status == "cancelled":
        return ""
    if status == "investigating":
        if has_pending_checks:
            return "Continue with the oldest pending check."
        if has_open_critical_findings:
            return "Investigate the unresolved critical findings."
        return "Update the summary and complete the task when the goal is met."
    return ""


async def task_context(db: AsyncSession, task_id: str) -> dict:
    from app.services.task_context import compile_task_context

    return await compile_task_context(db, task_id)


async def task_detail(db: AsyncSession, task: Task) -> dict:
    events = await db.execute(
        select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.created_at)
    )
    event_rows = list(events.scalars())
    findings = await db.execute(
        select(Finding).where(Finding.task_id == task.id).order_by(Finding.created_at)
    )
    checks = await db.execute(
        select(TaskCheck).where(TaskCheck.task_id == task.id).order_by(TaskCheck.created_at)
    )
    invocations = await db.execute(
        select(ToolInvocation)
        .where(ToolInvocation.task_id == task.id)
        .order_by(ToolInvocation.started_at)
    )
    return {
        **task_public(task, resolution_label=_task_resolution_label_from_events(event_rows)),
        "events": [
            {
                "id": event.id,
                "kind": event.kind,
                "payload": event.payload,
                "created_at": event.created_at,
            }
            for event in event_rows
        ],
        "findings": [finding_public(finding) for finding in findings.scalars()],
        "checks": [check_public(check) for check in checks.scalars()],
        "invocations": [
            {
                "id": invocation.id,
                "tool_id": invocation.tool_id,
                "status": invocation.status,
                "error_code": invocation.error_code,
                "started_at": invocation.started_at,
                "duration_ms": invocation.duration_ms,
            }
            for invocation in invocations.scalars()
        ],
    }


def task_public(task: Task, *, router_status: str = "", resolution_label: str = "") -> dict:
    effective_resolution_label = resolution_label if task.status == "completed" else ""
    return {
        "id": task.id,
        "title": task.title,
        "goal": task.goal,
        "status": task.status,
        "summary": task.summary,
        "source": task.source,
        "created_by": task.created_by,
        "assigned_agent": task.assigned_agent,
        "claimed_at": task.claimed_at,
        "last_activity_at": task.last_activity_at,
        "completed_at": task.completed_at,
        "version": task.version,
        "parent_task_id": task.parent_task_id,
        "router_status": router_status,
        "resolution_label": effective_resolution_label,
        "auto_closed": effective_resolution_label == "auto_closed",
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _task_resolution_label_from_events(events: list[TaskEvent]) -> str:
    for event in reversed(events):
        if event.kind == "watcher.task.auto_completed":
            return "auto_closed"
        if event.kind == "watcher.incident.resolve_handled":
            return "operator_handled"
        if event.kind == "task.operator_completed":
            return "human_handled"
    return ""


def finding_public(finding: Finding) -> dict:
    return {
        "id": finding.id,
        "task_id": finding.task_id,
        "severity": finding.severity,
        "title": finding.title,
        "description": finding.description,
        "source": finding.source,
        "tool_invocation_id": finding.tool_invocation_id,
        "created_by": finding.created_by,
        "created_at": finding.created_at,
        "resolved_at": finding.resolved_at,
    }


def resolution_public(resolution: TaskResolution) -> dict:
    return {
        "id": resolution.id,
        "task_id": resolution.task_id,
        "incident_id": resolution.incident_id,
        "summary": resolution.summary,
        "resolved_findings": resolution.resolved_findings,
        "resolved_by": resolution.resolved_by,
        "source": resolution.source,
        "created_at": resolution.created_at,
    }


def check_public(check: TaskCheck) -> dict:
    return {
        "id": check.id,
        "task_id": check.task_id,
        "description": check.description,
        "status": check.status,
        "skip_reason": check.skip_reason,
        "created_by": check.created_by,
        "completed_by": check.completed_by,
        "created_at": check.created_at,
        "completed_at": check.completed_at,
    }
