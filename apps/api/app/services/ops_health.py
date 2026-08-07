from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.locks import try_advisory_xact_lock
from app.db.models import (
    AuditEvent,
    Finding,
    NotificationOutbox,
    ProviderConfiguration,
    ToolInvocation,
    WatcherRun,
    utcnow,
)
from app.db.session import get_session_factory

logger = logging.getLogger("homelab.ops_health")


RETENTION_LOCK_NAME = "homelab.ops.retention"


async def retention_once() -> dict[str, int]:
    settings = get_settings()
    if not settings.operational_retention_enabled:
        return {}
    async with get_session_factory()() as db:
        if not await try_advisory_xact_lock(db, RETENTION_LOCK_NAME):
            await db.rollback()
            return {}
        deleted = await apply_retention(db)
        await db.commit()
        return deleted


async def apply_retention(db: AsyncSession) -> dict[str, int]:
    settings = get_settings()
    batch_size = max(1, settings.retention_batch_size)
    now = utcnow()
    plans = [
        ("audit_events", AuditEvent, AuditEvent.created_at, settings.audit_retention_days, []),
        (
            "tool_invocations",
            ToolInvocation,
            ToolInvocation.started_at,
            settings.tool_invocation_retention_days,
            [
                ToolInvocation.id.not_in(
                    select(Finding.tool_invocation_id).where(
                        Finding.tool_invocation_id.is_not(None)
                    )
                )
            ],
        ),
        ("watcher_runs", WatcherRun, WatcherRun.started_at, settings.watcher_run_retention_days, []),
        (
            "notification_outbox",
            NotificationOutbox,
            NotificationOutbox.created_at,
            settings.notification_outbox_retention_days,
            [NotificationOutbox.status.in_({"sent", "cancelled", "suppressed", "failed"})],
        ),
    ]
    deleted: dict[str, int] = {}
    for name, model, column, days, extra_filters in plans:
        if days <= 0:
            deleted[name] = 0
            continue
        cutoff = now - timedelta(days=days)
        ids = (
            await db.execute(
                select(model.id)
                .where(column < cutoff, *extra_filters)
                .order_by(column)
                .limit(batch_size)
            )
        ).scalars().all()
        if not ids:
            deleted[name] = 0
            continue
        result = await db.execute(delete(model).where(model.id.in_(ids)))
        deleted[name] = int(getattr(result, "rowcount", 0) or 0)
    return deleted


async def retention_loop() -> None:
    settings = get_settings()
    await asyncio.sleep(30)
    while True:
        try:
            deleted = await retention_once()
            if any(deleted.values()):
                logger.info("retention cleanup deleted rows: %s", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("retention cleanup failed")
        await asyncio.sleep(max(60, settings.retention_interval_seconds))


async def operational_health(db: AsyncSession) -> dict[str, Any]:
    settings = get_settings()
    notification_counts = (
        await db.execute(
            select(NotificationOutbox.status, func.count(NotificationOutbox.id)).group_by(NotificationOutbox.status)
        )
    ).all()
    last_watcher_run = (
        await db.execute(select(WatcherRun).order_by(WatcherRun.started_at.desc()).limit(1))
    ).scalar_one_or_none()
    provider_errors = (
        await db.execute(
            select(ProviderConfiguration)
            .where(
                ProviderConfiguration.last_error_at.is_not(None),
                ProviderConfiguration.last_status != "healthy",
            )
            .order_by(ProviderConfiguration.last_error_at.desc())
            .limit(10)
        )
    ).scalars().all()
    return {
        "database": await _database_health(db),
        "retention": {
            "enabled": settings.operational_retention_enabled,
            "interval_seconds": max(60, settings.retention_interval_seconds),
            "days": {
                "audit_events": settings.audit_retention_days,
                "tool_invocations": settings.tool_invocation_retention_days,
                "watcher_runs": settings.watcher_run_retention_days,
                "notification_outbox": settings.notification_outbox_retention_days,
            },
            "batch_size": max(1, settings.retention_batch_size),
        },
        "workers": {
            "watchers_enabled": settings.watchers_enabled,
            "notification_outbox_enabled": settings.notification_outbox_enabled,
            "notification_counts": {str(status): int(count) for status, count in notification_counts},
            "last_watcher_run": _watcher_public(last_watcher_run),
        },
        "provider_errors": [_provider_error_public(row) for row in provider_errors],
    }


async def _database_health(db: AsyncSession) -> dict[str, Any]:
    dialect = db.bind.dialect.name if db.bind else "unknown"
    result: dict[str, Any] = {"dialect": dialect}
    if dialect == "postgresql":
        size = await db.scalar(text("select pg_database_size(current_database())"))
        size_pretty = await db.scalar(text("select pg_size_pretty(pg_database_size(current_database()))"))
        connections = await db.scalar(
            text("select count(*) from pg_stat_activity where datname = current_database()")
        )
        result.update(
            {
                "size_bytes": int(size or 0),
                "size_pretty": str(size_pretty or ""),
                "connections": int(connections or 0),
            }
        )
    else:
        result.update({"size_bytes": None, "size_pretty": "", "connections": None})
    return result


def _watcher_public(row: WatcherRun | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "watcher_id": row.watcher_id,
        "status": row.status,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "created_tasks": row.created_tasks,
        "updated_incidents": row.updated_incidents,
        "resolved_incidents": row.resolved_incidents,
        "error": row.error,
    }


def _provider_error_public(row: ProviderConfiguration) -> dict[str, Any]:
    return {
        "provider_id": row.id,
        "display_name": row.display_name,
        "status": row.last_error_status,
        "message": row.last_error_detail,
        "at": row.last_error_at,
    }
