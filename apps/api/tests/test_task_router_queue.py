from datetime import timedelta

from sqlalchemy import select

from app.core.settings import get_settings
from app.db.models import TaskEvent, TaskRouterJob, utcnow
from app.domain.actors import Actor
from app.services.task_router import TaskRouterDecision, TaskRouterError, TaskRouterResult
from app.services.task_router_queue import enqueue_task_routing, process_once
from app.services.tasks_service import create_task, task_router_statuses


OPERATOR = Actor(kind="user", id="operator", label="Operator")


class FakeRouterModel:
    async def decide(self, context):
        assert context["context"]["trigger"] == "manual"
        return TaskRouterResult(
            model="opencode-go/deepseek-v4-pro",
            telemetry={"provider": "opencode_go", "inference_latency_ms": 12},
            decision=TaskRouterDecision(
                action="keep",
                category="network",
                priority="medium",
                severity="warning",
                suggested_owner="fixer",
                runbook="connectivity_alert",
                dedupe_candidate=None,
                summary="Investigate using the declared read-only runbook.",
                first_steps=["Read network summary"],
                labels=["network"],
                needs_operator=False,
                confidence=0.91,
            ),
        )


class TransientRouterModel:
    async def decide(self, _context):
        raise TaskRouterError(
            "provider busy",
            details={
                "transient": True,
                "telemetry": {"provider": "opencode_go", "error_kind": "http_error"},
            },
        )


async def test_enqueue_is_disabled_by_default(db_session):
    task = await create_task(db_session, "Router disabled", "No inference", OPERATOR)

    assert await enqueue_task_routing(db_session, task, OPERATOR, source="test") is None


async def test_async_router_job_is_idempotent_and_processed(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "task_router_enabled", True)
    task = await create_task(db_session, "Gateway warning", "Collect evidence", OPERATOR)
    first = await enqueue_task_routing(
        db_session,
        task,
        OPERATOR,
        source="test",
        context={"trigger": "manual"},
    )
    second = await enqueue_task_routing(
        db_session,
        task,
        OPERATOR,
        source="test",
        context={"trigger": "duplicate"},
    )
    await db_session.commit()

    assert first is not None
    assert second is not None
    assert second.id == first.id
    job_id = first.id
    task_id = task.id
    assert await process_once(model=FakeRouterModel()) is True

    db_session.expire_all()
    job = await db_session.get(TaskRouterJob, job_id)
    assert job is not None
    assert job.status == "succeeded"
    assert job.attempts == 1
    events = (
        await db_session.execute(
            select(TaskEvent).where(
                TaskEvent.task_id == task_id,
                TaskEvent.kind == "task.router_decision",
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert (await task_router_statuses(db_session, [task_id]))[task_id] == "routed"


async def test_stale_running_job_is_recovered(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "task_router_enabled", True)
    monkeypatch.setattr(settings, "task_router_job_lease_seconds", 30)
    task = await create_task(db_session, "Stale routing", "Recover the lease", OPERATOR)
    job = await enqueue_task_routing(
        db_session,
        task,
        OPERATOR,
        source="test",
        context={"trigger": "manual"},
    )
    assert job is not None
    job.status = "running"
    job.locked_at = utcnow() - timedelta(minutes=5)
    job_id = job.id
    await db_session.commit()

    assert await process_once(model=FakeRouterModel()) is True

    db_session.expire_all()
    recovered = await db_session.get(TaskRouterJob, job_id)
    assert recovered is not None
    assert recovered.status == "succeeded"
    assert recovered.attempts == 1


async def test_transient_failure_is_retried_without_terminal_event(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "task_router_enabled", True)
    monkeypatch.setattr(settings, "task_router_max_attempts", 3)
    task = await create_task(db_session, "Retry routing", "Provider is temporarily busy", OPERATOR)
    job = await enqueue_task_routing(
        db_session,
        task,
        OPERATOR,
        source="test",
        context={"trigger": "manual"},
    )
    assert job is not None
    job_id = job.id
    task_id = task.id
    await db_session.commit()

    assert await process_once(model=TransientRouterModel()) is True

    db_session.expire_all()
    retry = await db_session.get(TaskRouterJob, job_id)
    assert retry is not None
    assert retry.status == "pending"
    assert retry.attempts == 1
    failures = (
        await db_session.execute(
            select(TaskEvent).where(
                TaskEvent.task_id == task_id,
                TaskEvent.kind == "task.router_failed",
            )
        )
    ).scalars().all()
    assert failures == []


async def test_changed_task_blocks_watcher_policy_after_routing(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "task_router_enabled", True)
    task = await create_task(db_session, "Changed warning", "Do not auto-dispatch stale work", OPERATOR)
    job = await enqueue_task_routing(
        db_session,
        task,
        OPERATOR,
        source="test",
        context={"trigger": "manual"},
        policy_context={
            "kind": "watcher_auto_investigate",
            "watcher_id": "network.gateway",
            "investigation_mode": "auto_investigate",
            "severity": "warning",
            "runbook": "connectivity_alert",
            "match_outcome": "new",
            "match_method": "deterministic",
        },
    )
    assert job is not None
    task.version += 1
    await db_session.commit()

    assert await process_once(model=FakeRouterModel()) is True

    events = (
        await db_session.execute(
            select(TaskEvent).where(
                TaskEvent.task_id == task.id,
                TaskEvent.kind == "watcher.auto_investigate",
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["reason"] == "task_changed_since_routing"


async def test_stale_job_exhaustion_is_terminal(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "task_router_enabled", True)
    monkeypatch.setattr(settings, "task_router_max_attempts", 2)
    monkeypatch.setattr(settings, "task_router_job_lease_seconds", 30)
    monkeypatch.setattr(settings, "task_router_timeout_seconds", 1)
    monkeypatch.setattr(settings, "opencode_go_max_attempts", 1)
    task = await create_task(db_session, "Exhausted routing", "Do not loop forever", OPERATOR)
    job = await enqueue_task_routing(
        db_session,
        task,
        OPERATOR,
        source="test",
        context={"trigger": "manual"},
    )
    assert job is not None
    job.status = "running"
    job.attempts = 2
    job.lease_token = "expired-lease"
    job.locked_at = utcnow() - timedelta(minutes=2)
    job_id = job.id
    task_id = task.id
    await db_session.commit()

    assert await process_once(model=FakeRouterModel()) is False

    db_session.expire_all()
    exhausted = await db_session.get(TaskRouterJob, job_id)
    assert exhausted is not None
    assert exhausted.status == "failed"
    assert exhausted.error_code == "stale_attempts_exhausted"
    assert exhausted.lease_token == ""
    failures = (
        await db_session.execute(
            select(TaskEvent).where(
                TaskEvent.task_id == task_id,
                TaskEvent.kind == "task.router_failed",
            )
        )
    ).scalars().all()
    assert len(failures) == 1
