"""Durable asynchronous dispatch for schema-constrained task routing."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.locks import try_advisory_xact_lock
from app.db.models import Task, TaskEvent, TaskRouterJob, utcnow
from app.db.session import get_session_factory
from app.domain.actors import Actor, ActorKind
from app.services.audit import write_audit
from app.services.auto_investigation import POLICY_VERSION, maybe_auto_investigate
from app.services.redaction import redact
from app.services.task_router import (
    TaskRouterDecision,
    TaskRouterError,
    TaskRouterModel,
    record_task_router_failure,
    route_task,
)

logger = logging.getLogger("homelab.task_router_queue")
ACTOR_KINDS = {"user", "service", "agent", "telegram", "system"}


def _safe_json(value: Any) -> Any:
    return json.loads(json.dumps(redact(value), ensure_ascii=False, default=str))


def _job_actor(row: TaskRouterJob) -> Actor:
    kind = row.actor_kind if row.actor_kind in ACTOR_KINDS else "system"
    return Actor(kind=cast(ActorKind, kind), id=row.actor_id or "task-router", label=row.actor_label)


async def _record_queue_failure(
    db: AsyncSession,
    row: TaskRouterJob,
    *,
    code: str,
    message: str,
) -> None:
    payload = {
        "job_id": row.id,
        "error": {"code": code, "message": message},
        "attempts": row.attempts,
    }
    db.add(TaskEvent(task_id=row.task_id, kind="task.router_failed", payload=payload))
    await write_audit(
        db,
        actor=_job_actor(row),
        source=row.source,
        action="task.router_decision",
        outcome="error",
        task_id=row.task_id,
        metadata=payload,
    )


async def _owned_job(
    db: AsyncSession,
    *,
    row_id: str,
    lease_token: str,
) -> TaskRouterJob | None:
    return await db.scalar(
        select(TaskRouterJob)
        .where(
            TaskRouterJob.id == row_id,
            TaskRouterJob.status == "running",
            TaskRouterJob.lease_token == lease_token,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def enqueue_task_routing(
    db: AsyncSession,
    task: Task,
    actor: Actor,
    *,
    source: str,
    context: dict[str, Any] | None = None,
    policy_context: dict[str, Any] | None = None,
) -> TaskRouterJob | None:
    """Create at most one routing job in the caller's task transaction."""

    settings = get_settings()
    if not settings.conversation_enabled or not settings.task_router_enabled:
        return None
    row = TaskRouterJob(
        task_id=task.id,
        task_version=task.version,
        source=str(source)[:32],
        actor_kind=actor.kind,
        actor_id=actor.id[:80],
        actor_label=actor.label[:128],
        context=_safe_json(context or {}),
        policy_context=_safe_json(policy_context or {}),
        status="pending",
        available_at=utcnow(),
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        return await db.scalar(select(TaskRouterJob).where(TaskRouterJob.task_id == task.id))
    db.add(
        TaskEvent(
            task_id=task.id,
            kind="task.router_queued",
            payload={"job_id": row.id, "source": row.source},
        )
    )
    await db.flush()
    return row


async def _recover_stale_jobs(db: AsyncSession) -> None:
    settings = get_settings()
    lease_seconds = max(
        30,
        settings.task_router_job_lease_seconds,
        round(settings.task_router_timeout_seconds * settings.opencode_go_max_attempts) + 30,
    )
    cutoff = utcnow() - timedelta(seconds=lease_seconds)
    rows = (
        await db.execute(
            select(TaskRouterJob).where(
                TaskRouterJob.status == "running",
                TaskRouterJob.locked_at.is_not(None),
                TaskRouterJob.locked_at <= cutoff,
            )
        )
    ).scalars().all()
    for row in rows:
        row.lease_token = ""
        row.locked_at = None
        row.updated_at = utcnow()
        if row.attempts >= max(1, settings.task_router_max_attempts):
            row.status = "failed"
            row.finished_at = utcnow()
            row.error_code = "stale_attempts_exhausted"
            row.error_message = "routing lease expired after maximum attempts"
            await _record_queue_failure(
                db,
                row,
                code=row.error_code,
                message=row.error_message,
            )
        else:
            row.status = "pending"
            row.available_at = utcnow()
            row.error_code = "stale_job_recovered"


async def _apply_policy(
    db: AsyncSession,
    task: Task,
    actor: Actor,
    policy: dict[str, Any],
    decision,
) -> None:
    if policy.get("kind") != "watcher_auto_investigate":
        return
    required_strings = (
        "watcher_id",
        "investigation_mode",
        "severity",
        "match_outcome",
        "match_method",
    )
    if any(not isinstance(policy.get(key), str) for key in required_strings):
        logger.warning("invalid watcher routing policy for task %s", task.id)
        return
    runbook = policy.get("runbook")
    if runbook is not None and not isinstance(runbook, str):
        logger.warning("invalid watcher runbook policy for task %s", task.id)
        return
    await maybe_auto_investigate(
        db,
        task,
        actor,
        watcher_id=policy["watcher_id"],
        investigation_mode=policy["investigation_mode"],
        severity=policy["severity"],
        runbook=runbook,
        match_outcome=policy["match_outcome"],
        match_method=policy["match_method"],
        decision=decision,
    )


async def _record_policy_skip(
    db: AsyncSession,
    task: Task,
    actor: Actor,
    policy: dict[str, Any],
    decision: TaskRouterDecision,
    *,
    reason: str,
) -> None:
    if policy.get("kind") != "watcher_auto_investigate":
        return
    payload = {
        "policy": POLICY_VERSION,
        "outcome": "skipped",
        "reason": reason,
        "watcher_id": str(policy.get("watcher_id") or "")[:128],
        "router_owner": decision.suggested_owner,
        "router_confidence": decision.confidence,
        "read_only": True,
    }
    db.add(TaskEvent(task_id=task.id, kind="watcher.auto_investigate", payload=payload))
    await write_audit(
        db,
        actor=actor,
        source="watcher",
        action="watcher.auto_investigate",
        outcome="skipped",
        task_id=task.id,
        metadata=payload,
    )


def _safe_error_message(error: Exception) -> str:
    if isinstance(error, TaskRouterError):
        return str(error)[:500]
    return f"unexpected {error.__class__.__name__}"


async def process_once(*, model: TaskRouterModel | None = None) -> bool:
    """Claim one job without holding a database lock during inference."""

    settings = get_settings()
    if not settings.conversation_enabled or not settings.task_router_enabled:
        return False
    async with get_session_factory()() as db:
        if not await try_advisory_xact_lock(db, "homelab.task_router.worker"):
            await db.rollback()
            return False
        await _recover_stale_jobs(db)
        row = (
            await db.execute(
                select(TaskRouterJob)
                .where(
                    TaskRouterJob.status == "pending",
                    TaskRouterJob.available_at <= utcnow(),
                )
                .order_by(TaskRouterJob.available_at, TaskRouterJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            await db.commit()
            return False
        row.status = "running"
        row.attempts += 1
        row.lease_token = str(uuid4())
        row.locked_at = utcnow()
        row.updated_at = utcnow()
        row_id = row.id
        lease_token = row.lease_token
        await db.commit()

    async with get_session_factory()() as db:
        row = await db.get(TaskRouterJob, row_id)
        if row is None:
            return True
        task = await db.get(Task, row.task_id)
        if task is None:
            row.status = "failed"
            row.error_code = "task_missing"
            row.error_message = "routing task no longer exists"
            row.lease_token = ""
            row.locked_at = None
            row.finished_at = utcnow()
            await db.commit()
            return True
        actor = _job_actor(row)
        decision: TaskRouterDecision | None = None
        route_error: Exception | None = None
        try:
            decision = await route_task(
                db,
                task,
                actor,
                source=row.source,
                context=row.context,
                model=model,
                raise_on_error=True,
            )
        except Exception as exc:
            route_error = exc

        owned = await _owned_job(db, row_id=row_id, lease_token=lease_token)
        if owned is None:
            # A stale-lease recovery fenced this worker out while inference was
            # running. Discard every uncommitted decision/event from this try.
            await db.rollback()
            return True
        row = owned
        current_task = await db.scalar(
            select(Task)
            .where(Task.id == row.task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current_task is None:
            row.status = "failed"
            row.error_code = "task_missing"
            row.error_message = "routing task no longer exists"
            row.lease_token = ""
            row.locked_at = None
            row.finished_at = utcnow()
            await db.commit()
            return True
        task = current_task

        if route_error is not None:
            details = route_error.details if isinstance(route_error, TaskRouterError) else {}
            transient = bool(details.get("transient"))
            if transient and row.attempts < max(1, get_settings().task_router_max_attempts):
                telemetry_value = details.get("telemetry")
                telemetry = telemetry_value if isinstance(telemetry_value, dict) else {}
                row.status = "pending"
                row.available_at = utcnow() + timedelta(seconds=min(300, 5 * (2 ** row.attempts)))
                row.error_code = str(telemetry.get("error_kind") or "transient_error")[:64]
                row.error_message = _safe_error_message(route_error)
                row.lease_token = ""
                row.locked_at = None
                row.updated_at = utcnow()
                payload = {
                    "job_id": row.id,
                    "attempt": row.attempts,
                    "next_attempt_at": row.available_at.isoformat(),
                    "error_code": row.error_code,
                }
                db.add(TaskEvent(task_id=task.id, kind="task.router_retry_scheduled", payload=payload))
                await write_audit(
                    db,
                    actor=actor,
                    source=row.source,
                    action="task.router_retry",
                    outcome="scheduled",
                    task_id=task.id,
                    metadata=payload,
                )
                await db.commit()
                return True
            await record_task_router_failure(
                db,
                task,
                actor,
                source=row.source,
                context=row.context,
                error=route_error,
            )
            row.status = "failed"
            row.error_code = "task_router_failed"
            row.error_message = _safe_error_message(route_error)
        else:
            if decision is None:
                route_error = RuntimeError("task router returned no decision without an error")
                await record_task_router_failure(
                    db,
                    task,
                    actor,
                    source=row.source,
                    context=row.context,
                    error=route_error,
                )
                row.status = "failed"
                row.error_code = "task_router_failed"
                row.error_message = _safe_error_message(route_error)
                row.lease_token = ""
                row.locked_at = None
                row.finished_at = utcnow()
                row.updated_at = utcnow()
                await db.commit()
                return True
            row.status = "succeeded"
            row.error_code = ""
            row.error_message = ""
            try:
                if task.version != row.task_version or task.status != "open" or task.assigned_agent:
                    await _record_policy_skip(
                        db,
                        task,
                        actor,
                        row.policy_context or {},
                        decision,
                        reason="task_changed_since_routing",
                    )
                else:
                    await _apply_policy(db, task, actor, row.policy_context or {}, decision)
            except Exception as exc:
                logger.exception("post-routing policy failed for task %s", task.id)
                row.status = "policy_failed"
                row.error_code = "post_route_policy_failed"
                row.error_message = f"unexpected {exc.__class__.__name__}"
                payload = {
                    "job_id": row.id,
                    "error": {"code": row.error_code, "message": row.error_message},
                }
                db.add(TaskEvent(task_id=task.id, kind="task.router_policy_failed", payload=payload))
                await write_audit(
                    db,
                    actor=actor,
                    source=row.source,
                    action="task.router_policy",
                    outcome="error",
                    task_id=task.id,
                    metadata=payload,
                )
        row.lease_token = ""
        row.locked_at = None
        row.finished_at = utcnow()
        row.updated_at = utcnow()
        await db.commit()
    return True


async def worker_loop() -> None:
    while True:
        try:
            processed = await process_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("task router worker failed")
            processed = False
        await asyncio.sleep(
            0 if processed else max(0.2, get_settings().task_router_worker_interval_seconds)
        )
