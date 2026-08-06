"""Task lifecycle events routed into the persistent notification outbox."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.notification_outbox import enqueue, fingerprint

# Status transitions worth an interruption: something needs a human, or is
# now done. Routine agent bookkeeping (claimed/investigating/released) is
# deliberately excluded to keep this channel low-volume.
NOTIFY_STATUSES = {
    "waiting_operator": "⏸",
    "blocked": "\U0001f6ab",
}


async def notify_task_created(
    db: AsyncSession, title: str, source: str, *, task_id: str | None = None
) -> None:
    await enqueue(
        db,
        event_type="task.created",
        fingerprint_value=fingerprint("task", source, title),
        text=f"🆕 New task ({source}): {title}",
        severity="info",
        task_id=task_id,
        reply_markup=_task_keyboard(task_id),
        idempotency_suffix=task_id or "",
    )


async def notify_task_reopened(db: AsyncSession, title: str, *, task_id: str) -> None:
    await enqueue(
        db,
        event_type="task.reopened",
        fingerprint_value=fingerprint("task", task_id, "reopened"),
        text=f"↩️ Task reopened: {title}",
        severity="warning",
        task_id=task_id,
        idempotency_suffix=task_id,
    )


async def notify_status_change(
    db: AsyncSession, title: str, from_status: str, to_status: str, *, task_id: str
) -> None:
    icon = NOTIFY_STATUSES.get(to_status)
    if icon is None:
        return
    await enqueue(
        db,
        event_type=f"task.{to_status}",
        fingerprint_value=fingerprint("task", task_id, to_status),
        text=f"{icon} {title}: {from_status} → {to_status}",
        severity="critical",
        task_id=task_id,
        idempotency_suffix=f"{from_status}:{to_status}",
    )


async def notify_critical_finding(
    db: AsyncSession,
    task_title: str,
    finding_title: str,
    *,
    task_id: str | None = None,
) -> None:
    await enqueue(
        db,
        event_type="finding.critical",
        fingerprint_value=fingerprint("critical", task_id or task_title, finding_title),
        text=f"🔴 Critical finding in «{task_title}»: {finding_title}",
        severity="critical",
        task_id=task_id,
        reply_markup=_task_keyboard(task_id),
        idempotency_suffix=finding_title,
    )


def _task_keyboard(task_id: str | None) -> dict | None:
    if not task_id:
        return None
    return {
        "inline_keyboard": [
            [
                {"text": "Open task", "callback_data": f"task:detail:{task_id}"},
                {"text": "🛠 Assign to Fixer", "callback_data": f"task:assign_fixer:{task_id}"},
            ],
            [
                {"text": "Dashboard", "callback_data": "nav:home"},
            ]
        ]
    }
