from sqlalchemy import select

from app.db.models import TaskEvent, WatcherConfig
from app.domain.actors import Actor
from app.services.auto_investigation import maybe_auto_investigate
from app.services.fixer_dispatch import FIXER_AGENT_ID, FixerDispatchResult
from app.services.task_router import TaskRouterDecision
from app.services.tasks_service import claim_task, create_task
from app.services.watchers import configure_watcher, reset_runtime_state_for_tests, watcher_status


WATCHER = Actor(kind="service", id="watcher", label="Watcher")


def _decision(**patch) -> TaskRouterDecision:
    data = {
        "action": "keep",
        "category": "network",
        "priority": "high",
        "severity": "warning",
        "suggested_owner": "fixer",
        "runbook": "connectivity_alert",
        "dedupe_candidate": None,
        "summary": "Read-only connectivity investigation.",
        "first_steps": ["Read provider summary"],
        "labels": ["connectivity"],
        "needs_operator": False,
        "confidence": 0.91,
    }
    data.update(patch)
    return TaskRouterDecision.model_validate(data)


async def test_watcher_investigation_mode_is_persisted_and_reported(db_session):
    reset_runtime_state_for_tests()

    status = await configure_watcher(
        db_session,
        "cloudflare.tunnel",
        investigation_mode="auto_investigate",
    )

    row = await db_session.get(WatcherConfig, "cloudflare.tunnel")
    assert row is not None
    assert row.investigation_mode == "auto_investigate"
    item = next(item for item in status["watchers"] if item["id"] == "cloudflare.tunnel")
    assert item["investigation_mode"] == "auto_investigate"

    reset_runtime_state_for_tests()
    reloaded = await watcher_status(db_session)
    item = next(item for item in reloaded["watchers"] if item["id"] == "cloudflare.tunnel")
    assert item["investigation_mode"] == "auto_investigate"


async def test_auto_investigate_dispatches_only_after_policy_acceptance(db_session, monkeypatch):
    task = await create_task(db_session, "Tunnel degraded", "Investigate tunnel", WATCHER)

    async def fake_assign(db, task_id, actor, *, source):
        claimed = await claim_task(db, task_id, FIXER_AGENT_ID, actor, source=source)
        return claimed, FixerDispatchResult(True, "accepted", "accepted", 2)

    monkeypatch.setattr(
        "app.services.auto_investigation.assign_and_dispatch_fixer",
        fake_assign,
    )

    result = await maybe_auto_investigate(
        db_session,
        task,
        WATCHER,
        watcher_id="cloudflare.tunnel",
        investigation_mode="auto_investigate",
        severity="warning",
        runbook="connectivity_alert",
        match_outcome="new",
        match_method="deterministic",
        decision=_decision(),
    )

    assert result.dispatched is True
    assert task.status == "claimed"
    assert task.assigned_agent == FIXER_AGENT_ID
    events = (
        await db_session.execute(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id, TaskEvent.kind == "watcher.auto_investigate")
            .order_by(TaskEvent.created_at)
        )
    ).scalars().all()
    assert [event.payload["outcome"] for event in events] == ["requested", "dispatched"]
    assert all(event.payload["read_only"] is True for event in events)


async def test_auto_investigate_releases_task_when_delivery_fails(db_session, monkeypatch):
    task = await create_task(db_session, "Tunnel degraded", "Investigate tunnel", WATCHER)

    async def fake_assign(db, task_id, actor, *, source):
        claimed = await claim_task(db, task_id, FIXER_AGENT_ID, actor, source=source)
        return claimed, FixerDispatchResult(False, "dispatch_unreachable", "offline", 3)

    monkeypatch.setattr(
        "app.services.auto_investigation.assign_and_dispatch_fixer",
        fake_assign,
    )

    result = await maybe_auto_investigate(
        db_session,
        task,
        WATCHER,
        watcher_id="cloudflare.tunnel",
        investigation_mode="auto_investigate",
        severity="warning",
        runbook="connectivity_alert",
        match_outcome="new",
        match_method="deterministic",
        decision=_decision(),
    )

    assert result.dispatched is False
    assert result.reason == "dispatch_unreachable"
    assert task.status == "open"
    assert task.assigned_agent == ""
    events = (
        await db_session.execute(
            select(TaskEvent).where(
                TaskEvent.task_id == task.id,
                TaskEvent.kind == "watcher.auto_investigate",
            )
        )
    ).scalars().all()
    assert [event.payload["outcome"] for event in events] == ["requested", "failed"]


async def test_auto_investigate_skips_low_confidence_without_claiming(db_session):
    task = await create_task(db_session, "Tunnel degraded", "Investigate tunnel", WATCHER)

    result = await maybe_auto_investigate(
        db_session,
        task,
        WATCHER,
        watcher_id="cloudflare.tunnel",
        investigation_mode="auto_investigate",
        severity="warning",
        runbook="connectivity_alert",
        match_outcome="new",
        match_method="deterministic",
        decision=_decision(confidence=0.79),
    )

    assert result.dispatched is False
    assert result.reason == "router_confidence_too_low"
    assert task.status == "open"
    assert task.assigned_agent == ""


async def test_auto_investigate_records_missing_router_decision(db_session):
    task = await create_task(db_session, "Tunnel degraded", "Investigate tunnel", WATCHER)

    result = await maybe_auto_investigate(
        db_session,
        task,
        WATCHER,
        watcher_id="cloudflare.tunnel",
        investigation_mode="auto_investigate",
        severity="warning",
        runbook="connectivity_alert",
        match_outcome="new",
        match_method="deterministic",
        decision=None,
    )

    assert result.dispatched is False
    assert result.reason == "router_decision_missing"
    event = await db_session.scalar(
        select(TaskEvent).where(
            TaskEvent.task_id == task.id,
            TaskEvent.kind == "watcher.auto_investigate",
        )
    )
    assert event is not None
    assert event.payload["reason"] == "router_decision_missing"


async def test_auto_investigate_respects_dispatch_lock(db_session, monkeypatch):
    task = await create_task(db_session, "Tunnel degraded", "Investigate tunnel", WATCHER)

    async def lock_busy(_db, _name):
        return False

    monkeypatch.setattr("app.services.auto_investigation.try_advisory_xact_lock", lock_busy)
    result = await maybe_auto_investigate(
        db_session,
        task,
        WATCHER,
        watcher_id="cloudflare.tunnel",
        investigation_mode="auto_investigate",
        severity="warning",
        runbook="connectivity_alert",
        match_outcome="new",
        match_method="deterministic",
        decision=_decision(),
    )

    assert result.dispatched is False
    assert result.reason == "fixer_concurrency_lock_busy"
