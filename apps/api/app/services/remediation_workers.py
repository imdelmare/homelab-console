"""Durable, identity-bound remediation worker leases.

This module deliberately owns only worker jobs. Canonical task transitions stay
in tasks_service so a lease never becomes an alternate task state machine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import McpClient, Task, TaskWorkerIdempotency, TaskWorkerJob, utcnow
from app.db.session import get_session_factory
from app.domain.actors import Actor
from app.services.mcp_clients import TASK_WORKER_CAPABILITY, mcp_client_actor, mcp_client_has_capability
from app.services.tasks_service import (
    FINAL_TASK_STATUSES,
    RELEASABLE_STATUSES,
    TRANSITION_POLICY_RELEASE,
    TaskServiceError,
    _event,
    _expect_version,
    _locked_task,
    transition_task,
)

WORKER_OUTCOMES = frozenset({"completed", "released", "retry", "failed"})


@dataclass
class RemediationWorkerError(Exception):
    code: str
    message: str


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _lease_ttl_seconds() -> int:
    return max(30, min(900, int(get_settings().remediation_worker_lease_ttl_seconds)))


def _max_lease_lifetime_seconds() -> int:
    return max(
        _lease_ttl_seconds(),
        min(7200, int(get_settings().remediation_worker_max_lease_lifetime_seconds)),
    )


def _retry_seconds() -> int:
    return max(1, min(3600, int(get_settings().remediation_worker_retry_seconds)))


def _max_attempts() -> int:
    return max(1, min(10, int(get_settings().remediation_worker_max_attempts)))


def _poll_seconds() -> int:
    return max(1, min(60, int(get_settings().remediation_worker_poll_seconds)))


def _recovery_interval_seconds() -> float:
    return max(1.0, min(60.0, float(get_settings().remediation_worker_recovery_interval_seconds)))


def _token_hash(token: str) -> str:
    return hmac.new(
        get_settings().session_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _input_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _principal(client: McpClient) -> str:
    return client.principal_id


def _task_owner(principal_id: str) -> str:
    return f"agent:{principal_id}"


def _require_worker_client(client: McpClient | None) -> McpClient:
    if client is None:
        raise RemediationWorkerError("worker_capability_required", "worker capability is required")
    if client.revoked_at is not None:
        raise RemediationWorkerError("worker_client_revoked", "worker client is revoked")
    if not client.principal_id or not mcp_client_has_capability(client, TASK_WORKER_CAPABILITY):
        raise RemediationWorkerError("worker_capability_required", "worker capability is required")
    return client


def worker_job_public(job: TaskWorkerJob) -> dict:
    return {
        "job_id": job.id,
        "task_id": job.task_id,
        "task_version": job.task_version,
        "client_id": job.client_id,
        "principal_id": job.principal_id,
        "status": job.status,
        "attempt": job.attempt,
        "lease_generation": job.lease_generation,
        "lease_expires_at": job.lease_expires_at,
        "available_at": job.available_at,
        "error_code": job.error_code,
        "assigned_by": job.assigned_by,
        "assignment_kind": job.assignment_kind,
        "finished_at": job.finished_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _lease_public(job: TaskWorkerJob, token: str) -> dict:
    return {
        "job_id": job.id,
        "task_id": job.task_id,
        "task_version": job.task_version,
        "lease_token": token,
        "lease_expires_at": job.lease_expires_at,
        "attempt": job.attempt,
    }


async def assign_worker_task(
    db: AsyncSession,
    *,
    task_id: str,
    client_id: str,
    actor: Actor,
    expected_version: int | None = None,
    source: str = "rest",
    assignment_kind: str = "operator",
) -> tuple[Task, TaskWorkerJob]:
    if actor.kind not in {"user", "telegram"}:
        raise RemediationWorkerError("worker_capability_required", "operator assignment is required")
    client = _require_worker_client(await db.get(McpClient, client_id, with_for_update=True))
    task = await _locked_task(db, task_id)
    _expect_version(task, expected_version)
    if task.status != "open" or task.assigned_agent:
        raise RemediationWorkerError("worker_job_not_ready", "task is not open and unassigned")

    principal_id = _principal(client)
    owner = _task_owner(principal_id)
    task.assigned_agent = owner
    task.claimed_at = utcnow()
    await transition_task(
        db, task, "claimed", actor, source=source, reason="worker_assigned",
        details={"assigned_agent": owner, "client_id": client.id}, notify=False,
    )
    job = TaskWorkerJob(
        task_id=task.id,
        client_id=client.id,
        principal_id=principal_id,
        task_version=task.version,
        assigned_by=actor.audit_id(),
        assignment_kind=assignment_kind,
        available_at=utcnow(),
    )
    db.add(job)
    await db.flush()
    await _event(
        db, task, "task.worker_assigned",
        {"job_id": job.id, "client_id": client.id, "principal_id": principal_id, "assignment_kind": assignment_kind},
        actor, source,
    )
    return task, job


async def _recover_expired_jobs(db: AsyncSession, client: McpClient, now: datetime) -> None:
    jobs = list((await db.execute(
        select(TaskWorkerJob)
        .where(TaskWorkerJob.client_id == client.id, TaskWorkerJob.status == "running", TaskWorkerJob.lease_expires_at <= now)
        .with_for_update(skip_locked=True)
    )).scalars())
    for job in jobs:
        task = await _locked_task(db, job.task_id)
        actor = mcp_client_actor(client)
        await _event(
            db, task, "task.worker_lease_expired",
            {"job_id": job.id, "lease_generation": job.lease_generation}, actor, "mcp",
        )
        job.lease_token_hash = ""
        job.acquired_at = None
        job.lease_expires_at = None
        job.lease_max_expires_at = None
        if job.attempt >= _max_attempts():
            job.status = "failed"
            job.error_code = "worker_max_attempts"
            job.finished_at = now
            if task.status in RELEASABLE_STATUSES and task.assigned_agent == _task_owner(job.principal_id):
                await transition_task(
                    db, task, "open", actor, source="mcp", policy=TRANSITION_POLICY_RELEASE,
                    reason="worker_max_attempts", details={"job_id": job.id}, notify=False,
                )
                await _event(db, task, "task.worker_released", {"job_id": job.id, "reason": "worker_max_attempts"}, actor, "mcp")
        else:
            job.status = "pending"
            job.available_at = now + timedelta(seconds=_retry_seconds())


async def recover_expired_worker_jobs(
    db: AsyncSession, *, now: datetime | None = None, client_id: str | None = None
) -> None:
    """Recover every expired lease under the same client -> job -> task lock order."""
    now = now or utcnow()
    query = select(McpClient).where(
        McpClient.id.in_(
            select(TaskWorkerJob.client_id).where(
                TaskWorkerJob.status == "running", TaskWorkerJob.lease_expires_at <= now
            )
        )
    )
    if client_id:
        query = query.where(McpClient.id == client_id)
    clients = list((await db.execute(query.with_for_update(skip_locked=True))).scalars())
    for client in clients:
        await _recover_expired_jobs(db, client, now)


async def recovery_loop() -> None:
    """Core-owned bounded recovery; client polling only accelerates this path."""
    while True:
        try:
            async with get_session_factory()() as db:
                await recover_expired_worker_jobs(db)
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # The next bounded iteration retries transient database failures.
            pass
        await asyncio.sleep(_recovery_interval_seconds())


async def disable_worker_client_jobs(
    db: AsyncSession,
    *,
    client: McpClient,
    actor: Actor,
    reason: str,
    source: str = "rest",
) -> None:
    """Close active jobs before capability/token revocation commits.

    This function intentionally does not require an active capability: it is
    the operator-owned cleanup path used while revoking that capability.
    """
    now = utcnow()
    jobs = list(
        (
            await db.execute(
                select(TaskWorkerJob)
                .where(
                    TaskWorkerJob.client_id == client.id,
                    TaskWorkerJob.status.in_({"pending", "running"}),
                )
                .with_for_update()
            )
        ).scalars()
    )
    for job in jobs:
        task = await _locked_task(db, job.task_id)
        job.status = "failed"
        job.error_code = reason[:64]
        job.finished_at = now
        job.lease_token_hash = ""
        job.lease_expires_at = None
        job.lease_max_expires_at = None
        if task.status in RELEASABLE_STATUSES and task.assigned_agent == _task_owner(job.principal_id):
            await transition_task(
                db,
                task,
                "open",
                actor,
                source=source,
                policy=TRANSITION_POLICY_RELEASE,
                reason=reason,
                details={"job_id": job.id},
                notify=False,
            )
        await _event(
            db,
            task,
            "task.worker_disabled",
            {"job_id": job.id, "reason": reason},
            actor,
            source,
        )


async def worker_next(db: AsyncSession, *, client_id: str) -> dict:
    client = _require_worker_client(await db.get(McpClient, client_id, with_for_update=True))
    now = utcnow()
    await recover_expired_worker_jobs(db, client_id=client.id, now=now)
    job = (await db.execute(
        select(TaskWorkerJob)
        .where(TaskWorkerJob.client_id == client.id, TaskWorkerJob.status == "pending", TaskWorkerJob.available_at <= now)
        .order_by(TaskWorkerJob.available_at, TaskWorkerJob.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )).scalar_one_or_none()
    retry_after = _poll_seconds()
    if job is None:
        return {"job": None, "retry_after_seconds": retry_after}
    task = await _locked_task(db, job.task_id)
    if task.status in FINAL_TASK_STATUSES or task.assigned_agent != _task_owner(job.principal_id):
        job.status = "failed"
        job.error_code = "worker_job_not_ready"
        job.finished_at = now
        return {"job": None, "retry_after_seconds": retry_after}
    token = secrets.token_urlsafe(32)
    ttl = _lease_ttl_seconds()
    lifetime = _max_lease_lifetime_seconds()
    job.status = "running"
    job.attempt += 1
    job.lease_generation += 1
    job.lease_token_hash = _token_hash(token)
    job.acquired_at = now
    job.lease_expires_at = now + timedelta(seconds=ttl)
    job.lease_max_expires_at = now + timedelta(seconds=lifetime)
    await _event(db, task, "task.worker_lease_acquired", {"job_id": job.id, "lease_generation": job.lease_generation}, mcp_client_actor(client), "mcp")
    return {"job": _lease_public(job, token), "retry_after_seconds": retry_after}


async def _locked_job_for_client(db: AsyncSession, job_id: str, client_id: str) -> tuple[TaskWorkerJob, McpClient]:
    client = _require_worker_client(await db.get(McpClient, client_id, with_for_update=True))
    job = (await db.execute(select(TaskWorkerJob).where(TaskWorkerJob.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None:
        raise RemediationWorkerError("unknown_worker_job", "unknown worker job")
    if job.client_id != client.id or job.principal_id != _principal(client):
        raise RemediationWorkerError("invalid_worker_lease", "worker lease is not valid")
    return job, client


def _verify_active_lease(job: TaskWorkerJob, token: str, now: datetime) -> None:
    if job.status != "running" or not token or not secrets.compare_digest(job.lease_token_hash, _token_hash(token)):
        raise RemediationWorkerError("invalid_worker_lease", "worker lease is not valid")
    if job.lease_expires_at is None or _aware(job.lease_expires_at) <= now:
        raise RemediationWorkerError("worker_lease_expired", "worker lease has expired")


async def worker_renew(
    db: AsyncSession, *, client_id: str, job_id: str, lease_token: str, idempotency_key: str
) -> dict:
    job, _client = await _locked_job_for_client(db, job_id, client_id)
    now = utcnow()
    if not idempotency_key or len(idempotency_key) > 36:
        raise RemediationWorkerError("invalid_worker_lease", "invalid idempotency key")
    _verify_active_lease(job, lease_token, now)
    existing = await _idempotency_record(db, job, "renew", idempotency_key)
    if existing is not None:
        return dict(existing.result)
    max_expiry = _aware(job.lease_max_expires_at) if job.lease_max_expires_at else now
    new_expiry = min(now + timedelta(seconds=_lease_ttl_seconds()), max_expiry)
    if new_expiry <= now:
        raise RemediationWorkerError("worker_lease_expired", "worker lease maximum lifetime has elapsed")
    job.lease_expires_at = new_expiry
    result = {"job_id": job.id, "lease_expires_at": new_expiry.isoformat()}
    db.add(_new_idempotency_record(job, "renew", idempotency_key, "", result))
    return result


async def worker_finish(
    db: AsyncSession, *, client_id: str, job_id: str, lease_token: str, idempotency_key: str,
    outcome: str, expected_task_version: int, error_code: str = "",
) -> dict:
    if outcome not in WORKER_OUTCOMES:
        raise RemediationWorkerError("invalid_worker_outcome", "invalid worker outcome")
    if (outcome in {"retry", "failed"}) != bool(error_code) or len(error_code) > 64:
        raise RemediationWorkerError("invalid_worker_outcome", "invalid worker error code")
    payload = {"outcome": outcome, "expected_task_version": expected_task_version, "error_code": error_code}
    digest = _input_hash(payload)
    job, client = await _locked_job_for_client(db, job_id, client_id)
    now = utcnow()
    if not idempotency_key or len(idempotency_key) > 36:
        raise RemediationWorkerError("invalid_worker_lease", "invalid idempotency key")
    existing = await _idempotency_record(db, job, "finish", idempotency_key)
    if existing is not None:
        if existing.input_hash != digest:
            raise RemediationWorkerError("idempotency_conflict", "idempotency key was used with different input")
        if not secrets.compare_digest(job.lease_token_hash, _token_hash(lease_token)):
            raise RemediationWorkerError("invalid_worker_lease", "worker lease is not valid")
        return dict(existing.result)
    _verify_active_lease(job, lease_token, now)
    task = await _locked_task(db, job.task_id)
    try:
        _expect_version(task, expected_task_version)
    except TaskServiceError as exc:
        raise RemediationWorkerError("worker_lease_conflict", "task version does not match") from exc
    if outcome == "completed":
        valid = task.status == "completed"
    elif outcome == "released":
        valid = task.status == "open" and not task.assigned_agent
    elif outcome == "retry":
        valid = task.status not in FINAL_TASK_STATUSES and task.assigned_agent == _task_owner(job.principal_id)
    else:
        valid = task.status in {"blocked", "waiting_operator"} and task.assigned_agent == _task_owner(job.principal_id)
    if not valid:
        raise RemediationWorkerError("worker_job_not_ready", "task does not satisfy worker finish prerequisites")
    job.error_code = error_code
    job.lease_expires_at = None
    job.lease_max_expires_at = None
    if outcome == "retry":
        job.status = "pending"
        job.available_at = now + timedelta(seconds=_retry_seconds())
    else:
        job.status = outcome
        job.finished_at = now
    result = {"job_id": job.id, "outcome": outcome, "task_version": task.version}
    db.add(_new_idempotency_record(job, "finish", idempotency_key, digest, result))
    await _event(db, task, "task.worker_finished", {"job_id": job.id, "outcome": outcome, "error_code": error_code}, mcp_client_actor(client), "mcp")
    return result


async def _idempotency_record(
    db: AsyncSession, job: TaskWorkerJob, operation: str, idempotency_key: str
) -> TaskWorkerIdempotency | None:
    return (
        await db.execute(
            select(TaskWorkerIdempotency).where(
                TaskWorkerIdempotency.job_id == job.id,
                TaskWorkerIdempotency.lease_generation == job.lease_generation,
                TaskWorkerIdempotency.operation == operation,
                TaskWorkerIdempotency.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


def _new_idempotency_record(
    job: TaskWorkerJob, operation: str, idempotency_key: str, input_hash: str, result: dict
) -> TaskWorkerIdempotency:
    return TaskWorkerIdempotency(
        job_id=job.id,
        lease_generation=job.lease_generation,
        operation=operation,
        idempotency_key=idempotency_key,
        input_hash=input_hash,
        result=result,
    )


async def validate_worker_lease(
    db: AsyncSession, *, task_id: str, worker_job_id: str, worker_lease_token: str, client_id: str
) -> TaskWorkerJob:
    """Validate a live lease for future task and execution-core integrations."""
    job, _client = await _locked_job_for_client(db, worker_job_id, client_id)
    if job.task_id != task_id:
        raise RemediationWorkerError("invalid_worker_lease", "worker lease is not valid")
    _verify_active_lease(job, worker_lease_token, utcnow())
    task = await _locked_task(db, task_id)
    if task.assigned_agent != _task_owner(job.principal_id) or task.status in FINAL_TASK_STATUSES:
        raise RemediationWorkerError("worker_lease_conflict", "worker task ownership is no longer valid")
    return job
