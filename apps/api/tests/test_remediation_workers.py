import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models import McpClient, TaskEvent, TaskWorkerIdempotency, TaskWorkerJob, utcnow
from app.domain.actors import Actor
from app.db.session import get_session_factory
from app.services.fixer_dispatch import assign_and_dispatch_fixer
from app.services.mcp_clients import set_mcp_client_capabilities
from app.services.remediation_workers import (
    RemediationWorkerError,
    assign_worker_task,
    recover_expired_worker_jobs,
    validate_worker_lease,
    worker_finish,
    worker_next,
    worker_renew,
)
from app.services.tasks_service import TaskServiceError, create_task, release_task, set_status

OPERATOR = Actor(kind="user", id="operator", label="operator")


async def _worker(db_session, label="worker"):
    client = McpClient(
        agent_id="codex", client_label=label, host_fingerprint=label,
        token_hash=f"hash-{uuid4()}", token_hint=label, capabilities=["task-worker.v1"],
    )
    db_session.add(client)
    await db_session.flush()
    client.principal_id = f"worker:{client.id}"
    return client


async def _assigned_lease(db_session):
    client = await _worker(db_session)
    task = await create_task(db_session, "repair", "fix it", OPERATOR)
    task, job = await assign_worker_task(db_session, task_id=task.id, client_id=client.id, actor=OPERATOR)
    acquired = await worker_next(db_session, client_id=client.id)
    return client, task, job, acquired["job"]


async def test_assignment_is_principal_bound_and_next_returns_hashed_lease(db_session):
    client, task, job, lease = await _assigned_lease(db_session)
    other = await _worker(db_session, "same-family")

    assert task.assigned_agent == f"agent:worker:{client.id}"
    assert lease["job_id"] == job.id
    assert lease["lease_token"] != job.lease_token_hash
    assert (await worker_next(db_session, client_id=other.id))["job"] is None
    with pytest.raises(RemediationWorkerError) as exc:
        await validate_worker_lease(
            db_session, task_id=task.id, worker_job_id=job.id,
            worker_lease_token=lease["lease_token"], client_id=other.id,
        )
    assert exc.value.code == "invalid_worker_lease"


async def test_no_work_renew_replay_and_wrong_token(db_session):
    client = await _worker(db_session)
    assert (await worker_next(db_session, client_id=client.id))["job"] is None
    client, task, job, lease = await _assigned_lease(db_session)
    first = await worker_renew(
        db_session, client_id=client.id, job_id=job.id, lease_token=lease["lease_token"], idempotency_key="renew-1"
    )
    replay = await worker_renew(
        db_session, client_id=client.id, job_id=job.id, lease_token=lease["lease_token"], idempotency_key="renew-1"
    )
    assert replay == first
    with pytest.raises(RemediationWorkerError) as exc:
        await worker_renew(db_session, client_id=client.id, job_id=job.id, lease_token="wrong", idempotency_key="renew-2")
    assert exc.value.code == "invalid_worker_lease"


async def test_renew_idempotency_ledger_replays_a_after_b_without_extending(db_session):
    client, _task, job, lease = await _assigned_lease(db_session)
    first = await worker_renew(
        db_session, client_id=client.id, job_id=job.id, lease_token=lease["lease_token"], idempotency_key="renew-a"
    )
    second = await worker_renew(
        db_session, client_id=client.id, job_id=job.id, lease_token=lease["lease_token"], idempotency_key="renew-b"
    )
    replay = await worker_renew(
        db_session, client_id=client.id, job_id=job.id, lease_token=lease["lease_token"], idempotency_key="renew-a"
    )
    assert replay == first
    assert second["lease_expires_at"] >= first["lease_expires_at"]
    rows = (await db_session.execute(select(TaskWorkerIdempotency))).scalars().all()
    assert {(row.operation, row.idempotency_key) for row in rows} == {("renew", "renew-a"), ("renew", "renew-b")}


async def test_renew_never_exceeds_acquisition_lifetime(db_session):
    client, _task, job, lease = await _assigned_lease(db_session)
    maximum = utcnow() + timedelta(seconds=45)
    job.lease_max_expires_at = maximum
    await db_session.flush()

    renewed = await worker_renew(
        db_session,
        client_id=client.id,
        job_id=job.id,
        lease_token=lease["lease_token"],
        idempotency_key="bounded-renew",
    )

    assert job.lease_expires_at <= maximum
    assert renewed["lease_expires_at"] == job.lease_expires_at.isoformat()


async def test_renew_rejects_elapsed_maximum_lifetime(db_session):
    client, _task, job, lease = await _assigned_lease(db_session)
    job.lease_expires_at = utcnow() + timedelta(seconds=30)
    job.lease_max_expires_at = utcnow() - timedelta(seconds=1)
    await db_session.flush()

    with pytest.raises(RemediationWorkerError) as exc:
        await worker_renew(
            db_session,
            client_id=client.id,
            job_id=job.id,
            lease_token=lease["lease_token"],
            idempotency_key="elapsed-maximum",
        )

    assert exc.value.code == "worker_lease_expired"


async def test_expiry_fences_old_token_and_exhaustion_releases_task(db_session):
    client, task, job, lease = await _assigned_lease(db_session)
    job.lease_expires_at = utcnow() - timedelta(seconds=1)
    job.attempt = 3
    await db_session.flush()

    assert (await worker_next(db_session, client_id=client.id))["job"] is None
    assert job.status == "failed"
    assert task.status == "open"
    with pytest.raises(RemediationWorkerError) as exc:
        await validate_worker_lease(
            db_session, task_id=task.id, worker_job_id=job.id,
            worker_lease_token=lease["lease_token"], client_id=client.id,
        )
    assert exc.value.code == "invalid_worker_lease"
    events = (await db_session.execute(select(TaskEvent).where(TaskEvent.task_id == task.id))).scalars().all()
    assert "task.worker_lease_expired" in [event.kind for event in events]


async def test_core_recovery_recovers_expiry_without_client_polling(db_session):
    client, task, job, _lease = await _assigned_lease(db_session)
    job.lease_expires_at = utcnow() - timedelta(seconds=1)
    job.attempt = 3
    await db_session.flush()

    await recover_expired_worker_jobs(db_session)

    assert job.status == "failed"
    assert job.error_code == "worker_max_attempts"
    assert task.status == "open"


async def test_expiry_recovery_issues_new_fencing_token(db_session, monkeypatch):
    from app.core.settings import get_settings

    monkeypatch.setenv("REMEDIATION_WORKER_RETRY_SECONDS", "1")
    get_settings.cache_clear()
    try:
        client, task, job, old_lease = await _assigned_lease(db_session)
        job.lease_expires_at = utcnow() - timedelta(seconds=1)
        await db_session.flush()

        assert (await worker_next(db_session, client_id=client.id))["job"] is None
        assert job.status == "pending"
        job.available_at = utcnow() - timedelta(seconds=1)
        await db_session.flush()
        new_lease = (await worker_next(db_session, client_id=client.id))["job"]

        assert new_lease["attempt"] == 2
        assert new_lease["lease_token"] != old_lease["lease_token"]
        with pytest.raises(RemediationWorkerError) as exc:
            await validate_worker_lease(
                db_session,
                task_id=task.id,
                worker_job_id=job.id,
                worker_lease_token=old_lease["lease_token"],
                client_id=client.id,
            )
        assert exc.value.code == "invalid_worker_lease"
        assert await validate_worker_lease(
            db_session,
            task_id=task.id,
            worker_job_id=job.id,
            worker_lease_token=new_lease["lease_token"],
            client_id=client.id,
        ) is job
    finally:
        get_settings.cache_clear()


async def test_finish_checks_canonical_prerequisites_and_is_idempotent(db_session):
    client, task, job, lease = await _assigned_lease(db_session)
    worker = Actor(kind="agent", id=f"worker:{client.id}")
    with pytest.raises(RemediationWorkerError) as exc:
        await worker_finish(
            db_session, client_id=client.id, job_id=job.id, lease_token=lease["lease_token"],
            idempotency_key="finish-1", outcome="completed", expected_task_version=task.version,
        )
    assert exc.value.code == "worker_job_not_ready"

    await set_status(db_session, task.id, "investigating", worker, expected_version=task.version, source="mcp")
    await set_status(db_session, task.id, "completed", worker, expected_version=task.version, source="mcp")
    result = await worker_finish(
        db_session, client_id=client.id, job_id=job.id, lease_token=lease["lease_token"],
        idempotency_key="finish-1", outcome="completed", expected_task_version=task.version,
    )
    assert await worker_finish(
        db_session, client_id=client.id, job_id=job.id, lease_token=lease["lease_token"],
        idempotency_key="finish-1", outcome="completed", expected_task_version=task.version,
    ) == result
    with pytest.raises(RemediationWorkerError) as exc:
        await worker_finish(
            db_session, client_id=client.id, job_id=job.id, lease_token=lease["lease_token"],
            idempotency_key="finish-1", outcome="released", expected_task_version=task.version,
        )
    assert exc.value.code == "idempotency_conflict"


async def test_finish_released_requires_canonical_release(db_session):
    client, task, job, lease = await _assigned_lease(db_session)
    worker = Actor(kind="agent", id=f"worker:{client.id}")
    await release_task(
        db_session,
        task.id,
        worker,
        expected_version=task.version,
        handoff_summary="released by conformance test",
        source="mcp",
    )

    result = await worker_finish(
        db_session,
        client_id=client.id,
        job_id=job.id,
        lease_token=lease["lease_token"],
        idempotency_key="finish-released",
        outcome="released",
        expected_task_version=task.version,
    )

    assert result["outcome"] == "released"
    assert job.status == "released"


async def test_finish_retry_keeps_canonical_ownership_and_requeues(db_session):
    client, task, job, lease = await _assigned_lease(db_session)

    result = await worker_finish(
        db_session,
        client_id=client.id,
        job_id=job.id,
        lease_token=lease["lease_token"],
        idempotency_key="finish-retry",
        outcome="retry",
        expected_task_version=task.version,
        error_code="adapter_transient_failure",
    )

    assert result["outcome"] == "retry"
    assert job.status == "pending"
    assert job.error_code == "adapter_transient_failure"
    assert task.status == "claimed"
    assert task.assigned_agent == f"agent:worker:{client.id}"


async def test_finish_failed_requires_operator_visible_task_state(db_session):
    client, task, job, lease = await _assigned_lease(db_session)
    worker = Actor(kind="agent", id=f"worker:{client.id}")
    await set_status(
        db_session,
        task.id,
        "investigating",
        worker,
        expected_version=task.version,
        source="mcp",
    )
    await set_status(
        db_session,
        task.id,
        "waiting_operator",
        worker,
        expected_version=task.version,
        source="mcp",
    )

    result = await worker_finish(
        db_session,
        client_id=client.id,
        job_id=job.id,
        lease_token=lease["lease_token"],
        idempotency_key="finish-failed",
        outcome="failed",
        expected_task_version=task.version,
        error_code="operator_input_required",
    )

    assert result["outcome"] == "failed"
    assert job.status == "failed"
    assert task.status == "waiting_operator"


async def test_finish_rejects_error_code_outside_retry_and_failed(db_session):
    client, task, job, lease = await _assigned_lease(db_session)

    with pytest.raises(RemediationWorkerError) as exc:
        await worker_finish(
            db_session,
            client_id=client.id,
            job_id=job.id,
            lease_token=lease["lease_token"],
            idempotency_key="invalid-error-code",
            outcome="completed",
            expected_task_version=task.version,
            error_code="must_not_be_present",
        )

    assert exc.value.code == "invalid_worker_outcome"


async def test_legacy_fixer_and_pull_worker_cannot_own_the_same_task(db_session):
    client = await _worker(db_session)
    worker_task = await create_task(db_session, "worker repair", "pull only", OPERATOR)
    await assign_worker_task(
        db_session,
        task_id=worker_task.id,
        client_id=client.id,
        actor=OPERATOR,
    )

    with pytest.raises(TaskServiceError) as fixer_exc:
        await assign_and_dispatch_fixer(
            db_session,
            worker_task.id,
            OPERATOR,
            source="test",
        )
    assert fixer_exc.value.code == "task_already_claimed"

    fixer_task = await create_task(db_session, "fixer repair", "push only", OPERATOR)
    await assign_and_dispatch_fixer(db_session, fixer_task.id, OPERATOR, source="test")

    with pytest.raises(RemediationWorkerError) as worker_exc:
        await assign_worker_task(
            db_session,
            task_id=fixer_task.id,
            client_id=client.id,
            actor=OPERATOR,
        )
    assert worker_exc.value.code == "worker_job_not_ready"


async def test_capability_revocation_blocks_mutations(db_session):
    client, task, job, lease = await _assigned_lease(db_session)
    client.capabilities = []
    with pytest.raises(RemediationWorkerError) as exc:
        await worker_renew(
            db_session, client_id=client.id, job_id=job.id, lease_token=lease["lease_token"], idempotency_key="renew-1"
        )
    assert exc.value.code == "worker_capability_required"


async def test_concurrent_polling_delivers_one_lease(db_session):
    client = await _worker(db_session, "concurrent")
    task = await create_task(db_session, "repair once", "single delivery", OPERATOR)
    await assign_worker_task(
        db_session,
        task_id=task.id,
        client_id=client.id,
        actor=OPERATOR,
    )
    await db_session.commit()

    async def pull():
        async with get_session_factory()() as session:
            result = await worker_next(session, client_id=client.id)
            await session.commit()
            return result

    results = await asyncio.gather(pull(), pull())
    delivered = [result["job"] for result in results if result["job"] is not None]
    assert len(delivered) == 1


async def test_operator_capability_revocation_closes_job_and_releases_task(db_session):
    client, task, job, _lease = await _assigned_lease(db_session)

    await set_mcp_client_capabilities(
        db_session,
        client_id=client.id,
        capabilities=[],
        actor=OPERATOR,
    )
    await db_session.flush()

    assert job.status == "failed"
    assert job.error_code == "worker_capability_revoked"
    assert task.status == "open"
    assert task.assigned_agent == ""
