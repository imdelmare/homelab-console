from datetime import timedelta

from sqlalchemy import select

from app.core.settings import get_settings
from app.db.models import AuditEvent, Finding, NotificationOutbox, Task, ToolInvocation, WatcherRun, utcnow
from app.services.ops_health import apply_retention, operational_health


async def test_operational_health_reports_core_sections(db_session):
    health = await operational_health(db_session)

    assert health["database"]["dialect"] == "postgresql"
    assert "retention" in health
    assert "notification_counts" in health["workers"]
    assert health["workers"]["sentinel_heartbeat"]["enabled"] is False
    assert health["provider_errors"] == []


async def test_apply_retention_prunes_old_operational_rows(db_session, monkeypatch):
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "1")
    monkeypatch.setenv("TOOL_INVOCATION_RETENTION_DAYS", "1")
    monkeypatch.setenv("WATCHER_RUN_RETENTION_DAYS", "1")
    monkeypatch.setenv("NOTIFICATION_OUTBOX_RETENTION_DAYS", "1")
    monkeypatch.setenv("RETENTION_BATCH_SIZE", "50")
    get_settings.cache_clear()
    old = utcnow() - timedelta(days=3)
    fresh = utcnow()
    db_session.add_all(
        [
            AuditEvent(created_at=old, action="old.audit"),
            AuditEvent(created_at=fresh, action="fresh.audit"),
            ToolInvocation(
                started_at=old,
                actor_kind="service",
                actor_id="test",
                tool_id="old.tool",
                provider_id="test",
            ),
            ToolInvocation(
                started_at=fresh,
                actor_kind="service",
                actor_id="test",
                tool_id="fresh.tool",
                provider_id="test",
            ),
            WatcherRun(watcher_id="old.watcher", started_at=old),
            WatcherRun(watcher_id="fresh.watcher", started_at=fresh),
            NotificationOutbox(
                idempotency_key="old",
                fingerprint="old",
                event_type="test",
                text="old",
                status="sent",
                created_at=old,
            ),
            NotificationOutbox(
                idempotency_key="fresh",
                fingerprint="fresh",
                event_type="test",
                text="fresh",
                created_at=fresh,
            ),
        ]
    )
    await db_session.flush()

    deleted = await apply_retention(db_session)

    assert deleted == {
        "audit_events": 1,
        "tool_invocations": 1,
        "watcher_runs": 1,
        "notification_outbox": 1,
    }
    assert await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "fresh.audit"))
    assert await db_session.scalar(select(ToolInvocation).where(ToolInvocation.tool_id == "fresh.tool"))
    assert await db_session.scalar(select(WatcherRun).where(WatcherRun.watcher_id == "fresh.watcher"))
    assert await db_session.scalar(
        select(NotificationOutbox).where(NotificationOutbox.idempotency_key == "fresh")
    )


async def test_apply_retention_preserves_referenced_invocations_and_pending_notifications(
    db_session, monkeypatch
):
    monkeypatch.setenv("TOOL_INVOCATION_RETENTION_DAYS", "1")
    monkeypatch.setenv("NOTIFICATION_OUTBOX_RETENTION_DAYS", "1")
    get_settings.cache_clear()
    old = utcnow() - timedelta(days=3)
    task = Task(title="retention", goal="preserve evidence")
    invocation = ToolInvocation(
        started_at=old,
        actor_kind="service",
        actor_id="test",
        tool_id="referenced.tool",
        provider_id="test",
    )
    db_session.add_all([task, invocation])
    await db_session.flush()
    db_session.add_all(
        [
            Finding(
                task_id=task.id,
                title="evidence",
                tool_invocation_id=invocation.id,
            ),
            NotificationOutbox(
                idempotency_key="pending-old",
                fingerprint="pending-old",
                event_type="test",
                text="must still be delivered",
                status="pending",
                created_at=old,
            ),
        ]
    )
    await db_session.flush()

    deleted = await apply_retention(db_session)

    assert deleted["tool_invocations"] == 0
    assert deleted["notification_outbox"] == 0
    assert await db_session.get(ToolInvocation, invocation.id)
    assert await db_session.scalar(
        select(NotificationOutbox).where(
            NotificationOutbox.idempotency_key == "pending-old"
        )
    )
