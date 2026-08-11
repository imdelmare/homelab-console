import asyncio
from datetime import timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.db.models import Approval, AuditEvent, McpClient, TaskEvent, ToolInvocation, utcnow
from app.db.session import get_session_factory
from app.domain.actors import Actor
from app.services.approvals_service import input_digest, request_approval
from app.services.mcp_clients import revoke_mcp_client
from app.services.remediation_workers import assign_worker_task, recover_expired_worker_jobs, worker_next
from app.services.tasks_service import create_task
from app.tools import registry
from app.tools.execution import execute_tool
from app.tools.governance import APPROVED_WRITE_TOOLS
from app.tools.registry import ToolDefinition, EmptyInput

OPERATOR = Actor(kind="user", id="operator", label="operator")


class SecretishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = "value"


class EchoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: int


def _fake_tool(monkeypatch, **overrides) -> ToolDefinition:
    async def runner(_payload):
        return {"answer": 42}

    defaults = dict(
        id="test.echo",
        name="Test Echo",
        description="test tool",
        provider_id="test",
        category="test",
        mode="read",
        risk="low",
        enabled=True,
        timeout_seconds=5.0,
        input_model=EmptyInput,
        runner=runner,
    )
    defaults.update(overrides)
    tool = ToolDefinition.model_validate(defaults)
    monkeypatch.setattr(registry, "_TOOLS", [*registry._TOOLS, tool])
    return tool


async def test_unknown_tool_rejected():
    result = await execute_tool("no.such.tool", {}, OPERATOR)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "unknown_tool"


async def test_worker_execution_requires_identity_bound_live_lease(db_session, monkeypatch):
    _fake_tool(monkeypatch)
    worker_client = McpClient(
        agent_id="codex",
        client_label="Codex worker",
        host_fingerprint="worker-host",
        token_hash="execution-worker-hash",
        token_hint="worker",
        capabilities=["task-worker.v1"],
    )
    db_session.add(worker_client)
    await db_session.flush()
    worker_client.principal_id = f"worker:{worker_client.id}"
    task = await create_task(db_session, "repair", "test lease", OPERATOR)
    task, job = await assign_worker_task(
        db_session,
        task_id=task.id,
        client_id=worker_client.id,
        actor=OPERATOR,
    )
    acquired = await worker_next(db_session, client_id=worker_client.id)
    lease = acquired["job"]
    await db_session.commit()
    actor = Actor(kind="agent", id=worker_client.principal_id, label="worker")

    missing = await execute_tool("test.echo", {}, actor, task_id=task.id)
    assert missing.error is not None
    assert missing.error.code == "invalid_worker_lease"

    wrong_actor = await execute_tool(
        "test.echo",
        {},
        Actor(kind="agent", id="worker:another-client"),
        task_id=task.id,
        worker_client_id=worker_client.id,
        worker_job_id=job.id,
        worker_lease_token=lease["lease_token"],
    )
    assert wrong_actor.error is not None
    assert wrong_actor.error.code == "invalid_worker_lease"

    valid = await execute_tool(
        "test.echo",
        {},
        actor,
        task_id=task.id,
        worker_client_id=worker_client.id,
        worker_job_id=job.id,
        worker_lease_token=lease["lease_token"],
    )
    assert valid.ok is True


async def test_worker_approval_cannot_cross_lease_generation(db_session, monkeypatch):
    _fake_tool(monkeypatch, mode="write", risk="low")
    monkeypatch.setitem(APPROVED_WRITE_TOOLS, "test.echo", "docs/decisions/0004.md")
    worker_client = McpClient(
        agent_id="codex", client_label="worker", host_fingerprint="host", token_hash="worker-binding",
        token_hint="binding", capabilities=["task-worker.v1"],
    )
    db_session.add(worker_client)
    await db_session.flush()
    worker_client.principal_id = f"worker:{worker_client.id}"
    task = await create_task(db_session, "repair", "approval binding", OPERATOR)
    task, job = await assign_worker_task(db_session, task_id=task.id, client_id=worker_client.id, actor=OPERATOR)
    first_lease = (await worker_next(db_session, client_id=worker_client.id))["job"]
    actor = Actor(kind="agent", id=worker_client.principal_id, label="worker")
    approval = await request_approval(
        db_session, tool_id="test.echo", raw_input={}, actor=actor, task_id=task.id, source="mcp", worker_job=job,
    )
    approval.status = "approved"
    job.lease_expires_at = utcnow() - timedelta(seconds=1)
    await recover_expired_worker_jobs(db_session)
    job.available_at = utcnow() - timedelta(seconds=1)
    second_lease = (await worker_next(db_session, client_id=worker_client.id))["job"]
    await db_session.commit()

    assert job.lease_generation == 2
    rejected = await execute_tool(
        "test.echo", {}, actor, task_id=task.id, approval_id=approval.id,
        worker_client_id=worker_client.id, worker_job_id=job.id, worker_lease_token=second_lease["lease_token"],
    )
    assert rejected.error is not None
    assert rejected.error.code == "approval_required"
    # The old lease token never reaches persistence or the execution audit.
    assert first_lease["lease_token"] not in str(rejected.model_dump(mode="json"))

    current_approval = await request_approval(
        db_session, tool_id="test.echo", raw_input={}, actor=actor, task_id=task.id, source="mcp", worker_job=job,
    )
    current_approval.status = "approved"
    await revoke_mcp_client(db_session, client_id=worker_client.id, reason="test revoke", actor=OPERATOR)
    await db_session.commit()
    revoked = await execute_tool(
        "test.echo", {}, actor, task_id=task.id, approval_id=current_approval.id,
        worker_client_id=worker_client.id, worker_job_id=job.id, worker_lease_token=second_lease["lease_token"],
    )
    assert revoked.error is not None
    assert revoked.error.code == "worker_client_revoked"
    async with get_session_factory()() as db:
        assert (await db.get(Approval, current_approval.id)).status == "approved"


async def test_disabled_tool_rejected(monkeypatch):
    _fake_tool(monkeypatch, enabled=False)
    result = await execute_tool("test.echo", {}, OPERATOR)
    assert result.error is not None
    assert result.error.code == "tool_disabled"


async def test_unapproved_write_tool_forced_disabled(monkeypatch):
    _fake_tool(monkeypatch, mode="write")
    result = await execute_tool("test.echo", {}, OPERATOR)
    assert result.error is not None
    assert result.error.code == "tool_disabled"


async def test_medium_risk_requires_policy(monkeypatch):
    _fake_tool(monkeypatch, risk="medium")
    result = await execute_tool("test.echo", {}, OPERATOR)
    assert result.error is not None
    assert result.error.code == "policy_denied"


async def test_high_risk_requires_approval(monkeypatch):
    _fake_tool(monkeypatch, risk="high")
    result = await execute_tool("test.echo", {}, OPERATOR)
    assert result.error is not None
    assert result.error.code == "approval_required"


async def test_approved_write_tool_requires_approval_even_at_low_risk(monkeypatch):
    _fake_tool(monkeypatch, mode="write", risk="low")
    monkeypatch.setitem(APPROVED_WRITE_TOOLS, "test.echo", "docs/decisions/0004.md")
    result = await execute_tool("test.echo", {}, OPERATOR)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "approval_required"


async def test_input_bound_approval_only_unlocks_exact_input(monkeypatch):
    _fake_tool(monkeypatch, mode="write", risk="low", input_model=SecretishInput)
    monkeypatch.setitem(APPROVED_WRITE_TOOLS, "test.echo", "docs/decisions/0004.md")
    approved_input = {"token": "expected"}
    async with get_session_factory()() as db:
        approval = Approval(
            tool_id="test.echo",
            status="approved",
            requested_by="operator",
            input_hash=input_digest(SecretishInput.model_validate(approved_input)),
            expires_at=utcnow() + timedelta(minutes=5),
        )
        db.add(approval)
        await db.commit()
        approval_id = approval.id

    mismatched = await execute_tool(
        "test.echo", {"token": "other"}, OPERATOR, approval_id=approval_id
    )
    assert mismatched.ok is False
    assert mismatched.error is not None
    assert mismatched.error.code == "approval_required"

    matched = await execute_tool(
        "test.echo", approved_input, OPERATOR, approval_id=approval_id
    )
    assert matched.ok is True

    async with get_session_factory()() as db:
        stored = await db.get(Approval, approval_id)
        assert stored is not None
        assert stored.status == "consumed"
        assert stored.consumed_at is not None


async def test_high_risk_approval_is_consumed_once_under_concurrency(monkeypatch):
    calls = 0

    async def counted_runner(_payload):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"answer": 42}

    _fake_tool(monkeypatch, risk="high", runner=counted_runner)
    async with get_session_factory()() as db:
        approval = Approval(
            tool_id="test.echo",
            status="approved",
            requested_by="operator",
            expires_at=utcnow() + timedelta(minutes=5),
        )
        db.add(approval)
        await db.commit()
        approval_id = approval.id

    results = await asyncio.gather(
        *(execute_tool("test.echo", {}, OPERATOR, approval_id=approval_id) for _ in range(8))
    )

    assert calls == 1
    assert sum(result.ok for result in results) == 1
    assert all(result.ok or (result.error is not None and result.error.code == "approval_required") for result in results)
    async with get_session_factory()() as db:
        approval = await db.get(Approval, approval_id)
        assert approval is not None
        assert approval.status == "consumed"
        invocations = (
            await db.execute(
                select(ToolInvocation).where(ToolInvocation.approval_id == approval_id)
            )
        ).scalars().all()
        assert len(invocations) == 1


async def test_unknown_fields_rejected(monkeypatch):
    _fake_tool(monkeypatch)
    result = await execute_tool("test.echo", {"surprise": 1}, OPERATOR)
    assert result.error is not None
    assert result.error.code == "invalid_input"


async def test_invalid_input_rejected(monkeypatch):
    _fake_tool(monkeypatch, input_model=SecretishInput)
    result = await execute_tool("test.echo", {"token": 12}, OPERATOR)
    assert result.error is not None
    assert result.error.code == "invalid_input"
    # Validation errors must not echo submitted values.
    assert "12" not in result.error.message


async def test_unauthenticated_actor_rejected(monkeypatch):
    _fake_tool(monkeypatch)
    result = await execute_tool("test.echo", {}, Actor(kind="system", id="nobody"))
    assert result.error is not None
    assert result.error.code == "unauthorized"


async def test_timeout_handled(monkeypatch):
    async def slow_runner(_payload):
        await asyncio.sleep(1.0)
        return {}

    _fake_tool(monkeypatch, timeout_seconds=0.05, runner=slow_runner)
    result = await execute_tool("test.echo", {}, OPERATOR)
    assert result.error is not None
    assert result.error.code == "provider_timeout"


async def test_output_model_normalizes_and_rejects_invalid_provider_results(monkeypatch):
    _fake_tool(monkeypatch, output_model=EchoOutput)
    result = await execute_tool("test.echo", {}, OPERATOR)
    assert result.ok is True
    assert result.result == {"answer": 42}

    async def invalid_runner(_payload):
        return {"unexpected": True}

    _fake_tool(monkeypatch, id="test.invalid-output", output_model=EchoOutput, runner=invalid_runner)
    invalid = await execute_tool("test.invalid-output", {}, OPERATOR)
    assert invalid.ok is False
    assert invalid.error is not None
    assert invalid.error.code == "provider_error"
    assert invalid.error.message == "provider returned an invalid result"


async def test_secrets_redacted_everywhere(monkeypatch):
    async def leaky_runner(_payload):
        return {"password": "hunter2", "nested": {"api_key": "abc"}, "name": "ok"}

    _fake_tool(monkeypatch, input_model=SecretishInput, runner=leaky_runner)
    result = await execute_tool("test.echo", {"token": "super-secret"}, OPERATOR)

    assert result.ok is True
    assert result.result is not None
    assert result.result["password"] == "[REDACTED]"
    assert result.result["nested"]["api_key"] == "[REDACTED]"
    assert result.result["name"] == "ok"

    async with get_session_factory()() as db:
        audit = (await db.execute(select(AuditEvent).where(AuditEvent.action == "tool.run"))).scalars().all()
        assert audit, "audit event missing"
        meta = audit[-1].meta
        assert meta["input"]["token"] == "[REDACTED]"
        assert "super-secret" not in str(meta)

        invocation = (await db.execute(
            select(ToolInvocation).where(ToolInvocation.id == result.invocation_id)
        )).scalar_one()
        assert invocation.input_redacted["token"] == "[REDACTED]"


async def test_audit_and_task_event_generated(monkeypatch):
    from app.services.tasks_service import create_task

    _fake_tool(monkeypatch)
    async with get_session_factory()() as db:
        task = await create_task(db, "diagnose", "goal", OPERATOR)
        await db.commit()
        task_id = task.id
        version_before = task.version

    result = await execute_tool("test.echo", {}, OPERATOR, task_id=task_id)
    assert result.ok

    async with get_session_factory()() as db:
        events = (await db.execute(
            select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.kind == "task.tool_invoked")
        )).scalars().all()
        assert len(events) == 1
        assert events[0].payload == {
            "invocation_id": result.invocation_id,
            "tool_id": "test.echo",
            "status": "success",
            "duration_ms": result.duration_ms,
        }

        audit = (await db.execute(
            select(AuditEvent).where(AuditEvent.tool_id == "test.echo", AuditEvent.task_id == task_id)
        )).scalars().all()
        assert len(audit) == 1
        assert audit[0].outcome == "success"

        from app.services.tasks_service import get_task

        refreshed = await get_task(db, task_id)
        assert refreshed is not None
        assert refreshed.version == version_before + 1
        assert result.task_version == refreshed.version


async def test_tool_run_rejected_on_completed_task(monkeypatch):
    from app.services.tasks_service import create_task, set_status

    _fake_tool(monkeypatch)
    async with get_session_factory()() as db:
        task = await create_task(db, "diagnose", "goal", OPERATOR)
        await db.flush()
        await set_status(db, task.id, "claimed", OPERATOR)
        await set_status(db, task.id, "investigating", OPERATOR)
        await set_status(db, task.id, "completed", OPERATOR)
        await db.commit()
        task_id = task.id
        version_before = task.version

    result = await execute_tool("test.echo", {}, OPERATOR, task_id=task_id)
    assert result.error is not None
    assert result.error.code == "task_not_active"
    assert result.task_version == version_before

    async with get_session_factory()() as db:
        from app.services.tasks_service import get_task

        events = (await db.execute(
            select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.kind == "task.tool_invoked")
        )).scalars().all()
        assert events == []
        refreshed = await get_task(db, task_id)
        assert refreshed is not None
        assert refreshed.version == version_before


async def test_tool_run_rejected_for_non_owner_agent(monkeypatch):
    from app.services.tasks_service import claim_task, create_task

    _fake_tool(monkeypatch)
    claude = Actor(kind="agent", id="claude", label="Claude")
    codex = Actor(kind="agent", id="codex", label="Codex")
    async with get_session_factory()() as db:
        task = await create_task(db, "diagnose", "goal", OPERATOR)
        await db.flush()
        await claim_task(db, task.id, "agent:claude", claude)
        await db.commit()
        task_id = task.id

    result = await execute_tool("test.echo", {}, codex, task_id=task_id, source="mcp")
    assert result.error is not None
    assert result.error.code == "not_task_owner"

    # The task owner may still run tools against its own claimed task.
    result = await execute_tool("test.echo", {}, claude, task_id=task_id, source="mcp")
    assert result.ok
