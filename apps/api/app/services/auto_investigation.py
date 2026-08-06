"""Deterministic policy gate for watcher-triggered read-only investigations."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.locks import try_advisory_xact_lock
from app.db.models import Task, TaskEvent
from app.domain.actors import Actor
from app.services.audit import write_audit
from app.services.fixer_dispatch import FIXER_AGENT_ID, assign_and_dispatch_fixer
from app.services.task_router import TaskRouterDecision
from app.services.tasks_service import FINAL_TASK_STATUSES, TaskServiceError, release_task

POLICY_VERSION = "auto-investigate-v1"
MIN_ROUTER_CONFIDENCE = 0.80
ACTIVE_FIXER_STATUSES = {"claimed", "investigating", "waiting_operator", "blocked"}


@dataclass(frozen=True)
class AutoInvestigationResult:
    dispatched: bool
    reason: str


def _eligibility_reason(
    *,
    investigation_mode: str,
    severity: str,
    runbook: str | None,
    match_outcome: str,
    match_method: str,
    decision: TaskRouterDecision | None,
) -> str:
    if investigation_mode != "auto_investigate":
        return "manual_mode"
    if severity != "warning":
        return "severity_not_warning"
    if not runbook:
        return "runbook_missing"
    if match_outcome != "new" or match_method != "deterministic":
        return "incident_match_not_deterministic"
    if decision is None:
        return "router_decision_missing"
    if decision.action != "keep":
        return "router_action_not_keep"
    if decision.suggested_owner != "fixer":
        return "router_owner_not_fixer"
    if decision.needs_operator:
        return "operator_required"
    if decision.confidence < MIN_ROUTER_CONFIDENCE:
        return "router_confidence_too_low"
    if decision.runbook != runbook:
        return "router_runbook_mismatch"
    return "eligible"


async def _record_policy_event(
    db: AsyncSession,
    task: Task,
    actor: Actor,
    *,
    outcome: str,
    reason: str,
    watcher_id: str,
    decision: TaskRouterDecision | None,
) -> None:
    payload = {
        "policy": POLICY_VERSION,
        "outcome": outcome,
        "reason": reason,
        "watcher_id": watcher_id,
        "router_owner": decision.suggested_owner if decision else "",
        "router_confidence": decision.confidence if decision else None,
        "read_only": True,
    }
    db.add(TaskEvent(task_id=task.id, kind="watcher.auto_investigate", payload=payload))
    await write_audit(
        db,
        actor=actor,
        source="watcher",
        action="watcher.auto_investigate",
        outcome=outcome,
        task_id=task.id,
        metadata=payload,
    )


async def maybe_auto_investigate(
    db: AsyncSession,
    task: Task,
    actor: Actor,
    *,
    watcher_id: str,
    investigation_mode: str,
    severity: str,
    runbook: str | None,
    match_outcome: str,
    match_method: str,
    decision: TaskRouterDecision | None,
) -> AutoInvestigationResult:
    """Dispatch a new watcher task only after all v1 safety gates pass.

    Fixer is assigned before the narrow supervisor endpoint is called. If
    delivery fails, the task is released through the canonical task state
    machine so an operator can still pick it up normally.
    """

    reason = _eligibility_reason(
        investigation_mode=investigation_mode,
        severity=severity,
        runbook=runbook,
        match_outcome=match_outcome,
        match_method=match_method,
        decision=decision,
    )
    if reason == "manual_mode":
        return AutoInvestigationResult(False, reason)
    if reason != "eligible":
        await _record_policy_event(
            db,
            task,
            actor,
            outcome="skipped",
            reason=reason,
            watcher_id=watcher_id,
            decision=decision,
        )
        return AutoInvestigationResult(False, reason)

    if not await try_advisory_xact_lock(db, "homelab.auto_investigate.fixer"):
        reason = "fixer_concurrency_lock_busy"
        await _record_policy_event(
            db,
            task,
            actor,
            outcome="skipped",
            reason=reason,
            watcher_id=watcher_id,
            decision=decision,
        )
        return AutoInvestigationResult(False, reason)

    active_count = await db.scalar(
        select(func.count())
        .select_from(Task)
        .where(
            Task.id != task.id,
            Task.assigned_agent == FIXER_AGENT_ID,
            Task.status.in_(ACTIVE_FIXER_STATUSES),
        )
    )
    if int(active_count or 0) > 0:
        reason = "fixer_concurrency_limit"
        await _record_policy_event(
            db,
            task,
            actor,
            outcome="skipped",
            reason=reason,
            watcher_id=watcher_id,
            decision=decision,
        )
        return AutoInvestigationResult(False, reason)

    await _record_policy_event(
        db,
        task,
        actor,
        outcome="requested",
        reason="policy_accepted",
        watcher_id=watcher_id,
        decision=decision,
    )
    try:
        task, dispatch = await assign_and_dispatch_fixer(
            db,
            task.id,
            actor,
            source="watcher",
        )
    except TaskServiceError as exc:
        await _record_policy_event(
            db,
            task,
            actor,
            outcome="failed",
            reason=exc.code,
            watcher_id=watcher_id,
            decision=decision,
        )
        return AutoInvestigationResult(False, exc.code)

    if dispatch.ok:
        await _record_policy_event(
            db,
            task,
            actor,
            outcome="dispatched",
            reason=dispatch.code,
            watcher_id=watcher_id,
            decision=decision,
        )
        await db.commit()
        return AutoInvestigationResult(True, dispatch.code)

    if task.status not in FINAL_TASK_STATUSES and task.assigned_agent == FIXER_AGENT_ID:
        await release_task(
            db,
            task.id,
            actor,
            handoff_summary=f"Auto-investigate delivery failed: {dispatch.code}",
            source="watcher",
        )
    await _record_policy_event(
        db,
        task,
        actor,
        outcome="failed",
        reason=dispatch.code,
        watcher_id=watcher_id,
        decision=decision,
    )
    await db.commit()
    return AutoInvestigationResult(False, dispatch.code)
