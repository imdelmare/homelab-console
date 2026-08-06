"""Persistent, idempotent Telegram notification outbox."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.locks import try_advisory_xact_lock
from app.db.models import Incident, NotificationOutbox, Task, utcnow
from app.db.session import get_session_factory
from app.services.redaction import redact

logger = logging.getLogger("homelab.notification_outbox")

FINAL_STATUSES = {"sent", "failed", "suppressed", "cancelled"}


def fingerprint(*parts: str) -> str:
    normalized = ":".join(str(part).strip().lower() for part in parts if str(part).strip())
    return f"v1:{normalized}"[:192]


def _dedupe_group_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    known: set[str] = set()
    for item in items:
        incident_id = str(item.get("incident_id") or "")
        if incident_id and incident_id in known:
            continue
        result.append(redact(item))
        if incident_id:
            known.add(incident_id)
    return result


async def enqueue(
    db: AsyncSession,
    *,
    event_type: str,
    fingerprint_value: str,
    text: str,
    severity: str = "info",
    provider_id: str = "",
    task_id: str | None = None,
    reply_markup: dict | None = None,
    debounce_seconds: int = 0,
    idempotency_suffix: str = "",
    group_key: str = "",
    group_items: list[dict[str, Any]] | None = None,
) -> NotificationOutbox | None:
    settings = get_settings()
    if not settings.notification_outbox_enabled or not settings.telegram_notifications_enabled:
        return None
    group_items = _dedupe_group_items(group_items or [])
    now = utcnow()
    cooldown_seconds = (
        settings.notification_critical_cooldown_seconds
        if severity == "critical"
        else settings.notification_cooldown_seconds
    )
    if cooldown_seconds > 0:
        cooldown_cutoff = now - timedelta(seconds=cooldown_seconds)
        cooldown_filter = NotificationOutbox.fingerprint == fingerprint_value
        if group_key:
            cooldown_filter = or_(
                cooldown_filter, NotificationOutbox.group_key == group_key
            )
        recent = await db.scalar(
            select(func.count(NotificationOutbox.id)).where(
                cooldown_filter,
                NotificationOutbox.status == "sent",
                NotificationOutbox.sent_at >= cooldown_cutoff,
            )
        )
        if int(recent or 0):
            return await _record_suppressed(
                db,
                event_type=event_type,
                fingerprint_value=fingerprint_value,
                text=text,
                severity=severity,
                provider_id=provider_id,
                task_id=task_id,
                reason="cooldown",
                idempotency_suffix=idempotency_suffix,
                group_key=group_key,
                group_items=group_items,
            )
    if group_key:
        window_start = now - timedelta(
            seconds=max(0, settings.notification_aggregation_window_seconds)
        )
        grouped = (
            await db.execute(
                select(NotificationOutbox).where(
                    NotificationOutbox.group_key == group_key,
                    NotificationOutbox.status == "pending",
                    NotificationOutbox.created_at >= window_start,
                ).order_by(NotificationOutbox.created_at).limit(1)
            )
        ).scalar_one_or_none()
        if grouped is not None:
            existing = list(grouped.group_items or [])
            known = {str(item.get("incident_id") or "") for item in existing}
            for item in group_items:
                incident_id = str(item.get("incident_id") or "")
                if incident_id not in known:
                    existing.append(redact(item))
                    known.add(incident_id)
            grouped.group_items = existing
            if len(existing) > 1:
                grouped.reply_markup = {}
            if severity == "critical" and grouped.severity != "critical":
                grouped.severity = "critical"
                grouped.text = str(redact(text))[:4000]
            candidate_available_at = now + timedelta(seconds=max(0, debounce_seconds))
            if grouped.available_at > candidate_available_at:
                grouped.available_at = candidate_available_at
            grouped.updated_at = now
            return grouped
    key_material = f"{event_type}|{fingerprint_value}|{task_id or ''}|{idempotency_suffix}"
    key = hashlib.sha256(key_material.encode()).hexdigest()
    row = NotificationOutbox(
        idempotency_key=key,
        fingerprint=fingerprint_value,
        group_key=group_key,
        group_items=group_items,
        event_type=event_type,
        severity=severity,
        provider_id=provider_id,
        task_id=task_id,
        status="pending",
        text=str(redact(text))[:4000],
        reply_markup=redact(reply_markup or {}, provider_id="telegram"),
        available_at=now + timedelta(seconds=max(0, debounce_seconds)),
        max_attempts=max(1, settings.notification_max_attempts),
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        return None
    return row


async def enqueue_watcher_incident(
    db: AsyncSession,
    *,
    incident_id: str,
    dedupe_key: str,
    severity: str,
    provider_id: str,
    title: str,
    task_id: str | None,
    reply_markup: dict | None = None,
    watcher_id: str = "",
    group_key: str = "",
    immediate: bool = False,
) -> NotificationOutbox | None:
    settings = get_settings()
    delay = (
        0
        if immediate
        else settings.notification_critical_debounce_seconds
        if severity == "critical"
        else settings.notification_warning_debounce_seconds
    )
    icon = "🔴" if severity == "critical" else "⚠️"
    return await enqueue(
        db,
        event_type="watcher.incident",
        fingerprint_value=fingerprint(provider_id, dedupe_key),
        text=f"{icon} {title}",
        severity=severity,
        provider_id=provider_id,
        task_id=task_id,
        reply_markup=reply_markup,
        debounce_seconds=delay,
        idempotency_suffix=incident_id,
        group_key=group_key,
        group_items=[
            {
                "incident_id": incident_id,
                "watcher_id": watcher_id,
                "provider_id": provider_id,
                "severity": severity,
                "title": title[:256],
            }
        ],
    )


async def cancel_pending_for_incident(db: AsyncSession, *, provider_id: str, dedupe_key: str) -> int:
    rows = (
        await db.execute(
            select(NotificationOutbox).where(
                NotificationOutbox.fingerprint == fingerprint(provider_id, dedupe_key),
                NotificationOutbox.status == "pending",
            )
        )
    ).scalars().all()
    cancelled = 0
    for row in rows:
        if len(row.group_items or []) > 1:
            continue
        row.status = "cancelled"
        row.error_code = "resolved_during_debounce"
        row.updated_at = utcnow()
        cancelled += 1
    sent_rows = (
        await db.execute(
            select(NotificationOutbox).where(
                NotificationOutbox.status == "sent",
                NotificationOutbox.group_key != "",
            ).order_by(NotificationOutbox.sent_at.desc()).limit(100)
        )
    ).scalars().all()
    for row in sent_rows:
        if not any(
            str(item.get("incident_id") or "")
            for item in row.group_items or []
        ):
            continue
        incidents = [
            await db.get(Incident, str(item.get("incident_id")))
            for item in row.group_items or []
            if item.get("incident_id")
        ]
        if incidents and all(item is not None and item.status == "resolved" for item in incidents):
            row.status = "resolve_pending"
            row.available_at = utcnow()
            row.updated_at = utcnow()
    return cancelled


async def _record_suppressed(db: AsyncSession, **kwargs) -> NotificationOutbox | None:
    suffix = kwargs.pop("idempotency_suffix", "")
    reason = kwargs.pop("reason")
    key_material = f"suppressed|{kwargs['event_type']}|{kwargs['fingerprint_value']}|{kwargs.get('task_id') or ''}|{suffix}"
    row = NotificationOutbox(
        idempotency_key=hashlib.sha256(key_material.encode()).hexdigest(),
        fingerprint=kwargs.pop("fingerprint_value"),
        status="suppressed",
        error_code=reason,
        available_at=utcnow(),
        max_attempts=1,
        reply_markup={},
        **kwargs,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        return None
    return row


async def process_once() -> bool:
    settings = get_settings()
    if not settings.notification_outbox_enabled:
        return False
    async with get_session_factory()() as db:
        if not await try_advisory_xact_lock(db, "homelab.notification_outbox.worker"):
            await db.rollback()
            return False
        stale = (
            await db.execute(
                select(NotificationOutbox).where(
                    NotificationOutbox.status.in_({"sending", "editing"}),
                    NotificationOutbox.updated_at <= utcnow() - timedelta(minutes=5),
                )
            )
        ).scalars().all()
        for item in stale:
            item.status = "resolve_pending" if item.status == "editing" else "pending"
            item.error_code = "stale_delivery_recovered"
            item.available_at = utcnow()
        row = (
            await db.execute(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.status.in_({"pending", "resolve_pending"}),
                    NotificationOutbox.available_at <= utcnow(),
                )
                .order_by(NotificationOutbox.available_at, NotificationOutbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        if row.status == "resolve_pending":
            row.status = "editing"
            row.updated_at = utcnow()
            row_id = row.id
            chat_id = settings.telegram_allowed_chat_id
            message_id = row.telegram_message_id
            text = f"{row.text}\n\n✅ Resolved automatically"
            await db.commit()
            from app.services.telegram_service import edit_message_result

            ok, error_code = await edit_message_result(chat_id, message_id, text)
            async with get_session_factory()() as edit_db:
                edited = await edit_db.get(NotificationOutbox, row_id)
                if edited is not None:
                    edited.status = "sent" if ok else "failed"
                    edited.error_code = "auto_resolved" if ok else error_code
                    edited.updated_at = utcnow()
                    await edit_db.commit()
            return True
        active_items = []
        for item in row.group_items or []:
            incident = await db.get(Incident, str(item.get("incident_id")))
            if incident is not None and incident.status == "open":
                active_items.append(item)
        if row.group_items and not active_items:
            row.status = "cancelled"
            row.error_code = "resolved_during_debounce"
            await db.commit()
            return True
        if len(active_items) > 1:
            icon = "🔴" if row.severity == "critical" else "⚠️"
            titles = [str(item.get("title") or "incident") for item in active_items]
            row.text = (
                f"{icon} {len(active_items)} correlated incidents ({row.group_key})\n"
                + "\n".join(f"• {title}" for title in titles[:8])
            )[:4000]
            row.reply_markup = {}
        if row.task_id:
            task = await db.get(Task, row.task_id)
            if task is not None and task.status in {"completed", "cancelled"} and row.severity != "critical":
                row.status = "cancelled"
                row.error_code = "task_already_final"
                await db.commit()
                return True
        row.status = "sending"
        row.attempts += 1
        row.updated_at = utcnow()
        row_id = row.id
        chat_id = settings.telegram_allowed_chat_id
        text = row.text
        markup = row.reply_markup or None
        await db.commit()

    from app.services.telegram_service import send_message_result

    ok, message_id, error_code = await send_message_result(chat_id, text, markup)
    async with get_session_factory()() as db:
        row = await db.get(NotificationOutbox, row_id)
        if row is None:
            return True
        if ok:
            row.status = "sent"
            row.telegram_message_id = message_id
            row.sent_at = utcnow()
            row.error_code = ""
            row.error_message = ""
        elif row.attempts >= row.max_attempts:
            row.status = "failed"
            row.error_code = error_code or "delivery_failed"
        else:
            row.status = "pending"
            row.error_code = error_code or "delivery_failed"
            row.available_at = utcnow() + timedelta(seconds=min(300, 2 ** row.attempts * 5))
        row.updated_at = utcnow()
        await db.commit()
    return True


async def worker_loop() -> None:
    settings = get_settings()
    while True:
        try:
            processed = await process_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("notification outbox worker failed")
            processed = False
        await asyncio.sleep(0 if processed else max(0.5, settings.notification_worker_interval_seconds))


async def status_summary() -> dict[str, Any]:
    async with get_session_factory()() as db:
        counts = (await db.execute(select(NotificationOutbox.status, func.count()).group_by(NotificationOutbox.status))).all()
        outcomes = (
            await db.execute(
                select(NotificationOutbox.error_code, func.count())
                .where(NotificationOutbox.error_code != "")
                .group_by(NotificationOutbox.error_code)
            )
        ).all()
        grouped = await db.scalar(
            select(func.count(NotificationOutbox.id)).where(
                NotificationOutbox.group_key != ""
            )
        )
        last_sent = (
            await db.execute(
                select(NotificationOutbox).where(NotificationOutbox.status == "sent").order_by(NotificationOutbox.sent_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        return {
            "enabled": get_settings().notification_outbox_enabled,
            "counts": {status: count for status, count in counts},
            "outcomes": {code: count for code, count in outcomes},
            "grouped": int(grouped or 0),
            "last_sent_at": _iso(last_sent.sent_at) if last_sent else None,
        }


async def list_recent(limit: int = 25) -> dict[str, Any]:
    async with get_session_factory()() as db:
        rows = (
            await db.execute(select(NotificationOutbox).order_by(NotificationOutbox.created_at.desc()).limit(max(1, min(limit, 100))))
        ).scalars().all()
        return {"items": [_public(row) for row in rows]}


def _public(row: NotificationOutbox) -> dict[str, Any]:
    return {
        "id": row.id,
        "fingerprint": row.fingerprint,
        "group_key": row.group_key,
        "group_size": len(row.group_items or []),
        "event_type": row.event_type,
        "severity": row.severity,
        "provider_id": row.provider_id,
        "task_id": row.task_id,
        "status": row.status,
        "available_at": _iso(row.available_at),
        "attempts": row.attempts,
        "error_code": row.error_code,
        "telegram_message_id": row.telegram_message_id,
        "sent_at": _iso(row.sent_at),
        "created_at": _iso(row.created_at),
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()
