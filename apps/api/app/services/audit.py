"""Structured, redacted audit logging backed by the database.

An optional JSONL sink can be enabled for development
(``AUDIT_JSONL_ENABLED=true``); the database remains the source of truth.
"""

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import AuditEvent
from app.domain.actors import Actor
from app.services.redaction import redact

logger = logging.getLogger("homelab.audit")

SYSTEM_ACTOR = Actor(kind="system", id="system", label="system")


async def write_audit(
    db: AsyncSession,
    *,
    actor: Actor,
    source: str,
    action: str,
    outcome: str,
    tool_id: str = "",
    task_id: str = "",
    request_id: str = "",
    duration_ms: int | None = None,
    metadata: dict[str, Any] | None = None,
    provider_id: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_kind=actor.kind,
        actor_id=actor.id,
        source=source,
        request_id=request_id,
        task_id=task_id or "",
        action=action,
        tool_id=tool_id,
        outcome=outcome,
        duration_ms=duration_ms,
        meta=redact(metadata or {}, provider_id=provider_id),
    )
    db.add(event)
    await db.flush()

    settings = get_settings()
    if settings.audit_jsonl_enabled:
        _append_jsonl(event)

    return event


def _append_jsonl(event: AuditEvent) -> None:
    settings = get_settings()
    path = Path(__file__).resolve().parents[3] / settings.audit_jsonl_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "id": event.id,
            "created_at": event.created_at.isoformat() if event.created_at else None,
            "actor": f"{event.actor_kind}:{event.actor_id}",
            "source": event.source,
            "action": event.action,
            "tool_id": event.tool_id,
            "task_id": event.task_id,
            "outcome": event.outcome,
            "duration_ms": event.duration_ms,
            "metadata": event.meta,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    except OSError:
        logger.warning("audit JSONL sink unavailable", exc_info=True)


async def read_audit(db: AsyncSession, limit: int = 100) -> list[AuditEvent]:
    limit = max(1, min(limit, 500))
    result = await db.execute(
        select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
    )
    return list(result.scalars())
