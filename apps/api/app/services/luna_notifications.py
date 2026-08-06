"""Low-volume operational alerts for the Luna task router."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NotificationOutbox, TaskEvent
from app.services.notification_outbox import enqueue, fingerprint

FAILURE_EVENT = "luna.router.failed"
RECOVERY_EVENT = "luna.router.recovered"
FAILURE_GROUP = "luna:task-router:degraded"


def _failure_reason(message: str, details: dict[str, Any]) -> str:
    status = str(details.get("http_status") or "").strip()
    if status == "429":
        return "quota or rate limit (HTTP 429)"
    if details.get("response_status") == "incomplete":
        return "incomplete model response"
    if status and status != "200":
        return f"provider HTTP error {status}"
    return message[:160] or "unclassified error"


async def notify_router_failure(
    db: AsyncSession,
    *,
    task_id: str,
    model: str,
    message: str,
    details: dict[str, Any],
) -> None:
    reason = _failure_reason(message, details)
    latest_recovery = await db.scalar(
        select(NotificationOutbox)
        .where(NotificationOutbox.event_type == RECOVERY_EVENT)
        .order_by(NotificationOutbox.created_at.desc())
        .limit(1)
    )
    episode = latest_recovery.id if latest_recovery is not None else "initial"
    await enqueue(
        db,
        event_type=FAILURE_EVENT,
        fingerprint_value=fingerprint("luna", "task-router", "degraded", episode),
        group_key=f"{FAILURE_GROUP}:{episode}",
        text=(
            "⚠️ Luna Task Router degraded\n\n"
            f"Error: {reason}\n"
            f"Model: {model}\n"
            f"Task: {task_id}\n"
            "Tasks continue without automatic routing."
        ),
        severity="critical",
        provider_id="openai",
        task_id=task_id,
        debounce_seconds=0,
        idempotency_suffix=task_id,
    )


async def notify_router_recovery(db: AsyncSession, *, model: str) -> None:
    latest_recovery = await db.scalar(
        select(NotificationOutbox)
        .where(NotificationOutbox.event_type == RECOVERY_EVENT)
        .order_by(NotificationOutbox.created_at.desc())
        .limit(1)
    )
    failure_query = select(NotificationOutbox).where(
        NotificationOutbox.event_type == FAILURE_EVENT
    )
    if latest_recovery is not None:
        failure_query = failure_query.where(
            NotificationOutbox.created_at > latest_recovery.created_at
        )
    first_failure_alert = await db.scalar(
        failure_query
        .order_by(NotificationOutbox.created_at.asc())
        .limit(1)
    )
    if first_failure_alert is None:
        return
    recovery_already_queued = await db.scalar(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.event_type == RECOVERY_EVENT,
            NotificationOutbox.created_at >= first_failure_alert.created_at,
        )
        .limit(1)
    )
    if recovery_already_queued is not None:
        return
    failures = await db.scalar(
        select(func.count(TaskEvent.id)).where(
            TaskEvent.kind == "task.router_failed",
            TaskEvent.created_at > first_failure_alert.created_at,
        )
    )
    await enqueue(
        db,
        event_type=RECOVERY_EVENT,
        fingerprint_value=fingerprint("luna", "task-router", "recovered", first_failure_alert.id),
        text=(
            "✅ Luna Task Router recovered\n\n"
            f"Model: {model}\n"
            f"Failures during degradation: {1 + int(failures or 0)}\n"
            "Automatic routing is operational again."
        ),
        severity="info",
        provider_id="openai",
        debounce_seconds=0,
        idempotency_suffix=first_failure_alert.id,
    )
