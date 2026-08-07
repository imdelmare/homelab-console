"""Narrow operator-controlled dispatch to the OpenCode-powered Fixer."""

from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import TaskEvent
from app.domain.actors import Actor
from app.services.audit import write_audit
from app.services.tasks_service import (
    TaskServiceError,
    claim_task,
    get_task,
    record_fixer_dispatch_requested,
)

FIXER_AGENT_ID = "agent:fixer"
DISPATCHABLE_STATUSES = {"claimed", "investigating", "waiting_operator", "blocked"}


@dataclass(frozen=True)
class FixerDispatchResult:
    ok: bool
    code: str
    message: str
    duration_ms: int

    def public(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "duration_ms": self.duration_ms,
        }


def _validated_endpoint() -> str:
    endpoint = get_settings().fixer_dispatch_url.strip()
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path != "/fixer":
        raise ValueError("FIXER_DISPATCH_URL must be an http(s) URL ending in /fixer")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("FIXER_DISPATCH_URL contains unsupported components")
    return endpoint


async def assign_and_dispatch_fixer(
    db: AsyncSession,
    task_id: str,
    actor: Actor,
    *,
    source: str,
):
    task = await get_task(db, task_id)
    if task is None:
        raise TaskServiceError("unknown_task", "unknown task")

    if task.status == "open" and not task.assigned_agent:
        task = await claim_task(db, task_id, FIXER_AGENT_ID, actor, source=source)
    elif task.assigned_agent != FIXER_AGENT_ID:
        raise TaskServiceError("task_already_claimed", "task is not assigned to Fixer")
    elif task.status not in DISPATCHABLE_STATUSES:
        raise TaskServiceError("invalid_transition", "task is not in a dispatchable state")

    authorization = await record_fixer_dispatch_requested(db, task, actor, source=source)

    # The supervisor immediately fetches the task over MCP. Commit ownership
    # and authorization before waking it so it can never observe partial state.
    await db.commit()
    result = await dispatch_fixer(
        db,
        task.id,
        actor,
        source=source,
        authorization_event_id=authorization.id,
    )
    await db.commit()
    return task, result


async def dispatch_fixer(
    db: AsyncSession,
    task_id: str,
    actor: Actor,
    *,
    source: str,
    authorization_event_id: str,
) -> FixerDispatchResult:
    task = await get_task(db, task_id)
    authorization = await db.get(TaskEvent, authorization_event_id)
    if (
        task is None
        or task.assigned_agent != FIXER_AGENT_ID
        or task.status not in DISPATCHABLE_STATUSES
        or authorization is None
        or authorization.task_id != task_id
        or authorization.kind != "task.fixer_dispatch_requested"
        or authorization.payload.get("assigned_agent") != FIXER_AGENT_ID
        or authorization.payload.get("authorized_by") != actor.audit_id()
    ):
        raise TaskServiceError(
            "dispatch_not_authorized",
            "Fixer dispatch is not bound to a valid authorization event",
        )

    settings = get_settings()
    started = monotonic()

    if not settings.fixer_dispatch_enabled:
        result = FixerDispatchResult(False, "dispatch_disabled", "Fixer dispatch is disabled", 0)
    elif not settings.fixer_dispatch_secret:
        result = FixerDispatchResult(False, "dispatch_not_configured", "Fixer dispatch secret is missing", 0)
    else:
        try:
            endpoint = _validated_endpoint()
            timeout = max(1.0, min(float(settings.fixer_dispatch_timeout_seconds), 15.0))
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    endpoint,
                    json={"task_id": task_id},
                    headers={"X-Secret": settings.fixer_dispatch_secret},
                )
            duration_ms = int((monotonic() - started) * 1000)
            if 200 <= response.status_code < 300:
                result = FixerDispatchResult(True, "accepted", "Fixer accepted the task", duration_ms)
            elif response.status_code in {401, 403}:
                result = FixerDispatchResult(False, "dispatch_unauthorized", "Fixer rejected authentication", duration_ms)
            elif response.status_code == 409:
                result = FixerDispatchResult(False, "already_running", "Fixer reports the task is already running", duration_ms)
            else:
                result = FixerDispatchResult(False, "dispatch_rejected", f"Fixer returned HTTP {response.status_code}", duration_ms)
        except (httpx.TimeoutException, httpx.NetworkError):
            result = FixerDispatchResult(
                False,
                "dispatch_unreachable",
                "Fixer supervisor is unreachable",
                int((monotonic() - started) * 1000),
            )
        except ValueError as exc:
            result = FixerDispatchResult(False, "dispatch_not_configured", str(exc), 0)

    await write_audit(
        db,
        actor=actor,
        source=source,
        action="fixer.dispatch",
        outcome="success" if result.ok else result.code,
        task_id=task_id,
        duration_ms=result.duration_ms,
        metadata={"code": result.code},
    )
    return result
