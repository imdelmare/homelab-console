from datetime import timedelta

from sqlalchemy import select

from app.core.settings import get_settings
from app.db.models import NotificationOutbox, utcnow
from app.services.notification_outbox import (
    cancel_pending_for_incident,
    enqueue,
    enqueue_watcher_incident,
    fingerprint,
    process_once,
)


def enable_outbox(monkeypatch):
    monkeypatch.setenv("TELEGRAM_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_COOLDOWN_SECONDS", "7200")
    monkeypatch.setenv("NOTIFICATION_CRITICAL_DEBOUNCE_SECONDS", "0")
    monkeypatch.setenv("NOTIFICATION_CRITICAL_COOLDOWN_SECONDS", "1800")
    monkeypatch.setenv("NOTIFICATION_AGGREGATION_WINDOW_SECONDS", "120")
    get_settings.cache_clear()


async def test_enqueue_is_idempotent(db_session, monkeypatch):
    enable_outbox(monkeypatch)

    first = await enqueue(
        db_session,
        event_type="task.blocked",
        fingerprint_value="v1:task:one:blocked",
        text="blocked",
        severity="critical",
        task_id=None,
        idempotency_suffix="one",
    )
    second = await enqueue(
        db_session,
        event_type="task.blocked",
        fingerprint_value="v1:task:one:blocked",
        text="blocked",
        severity="critical",
        task_id=None,
        idempotency_suffix="one",
    )

    assert first is not None
    assert second is None
    assert len((await db_session.execute(select(NotificationOutbox))).scalars().all()) == 1


async def test_noncritical_recent_delivery_is_persistently_suppressed(db_session, monkeypatch):
    enable_outbox(monkeypatch)
    delivered = NotificationOutbox(
        idempotency_key="delivered",
        fingerprint="v1:uptimekuma:monitor:pizero1",
        event_type="watcher.incident",
        severity="warning",
        status="sent",
        text="old",
        sent_at=utcnow() - timedelta(minutes=10),
    )
    db_session.add(delivered)
    await db_session.flush()

    row = await enqueue(
        db_session,
        event_type="watcher.incident",
        fingerprint_value=delivered.fingerprint,
        text="repeat",
        severity="warning",
        idempotency_suffix="repeat",
    )

    assert row is not None
    assert row.status == "suppressed"
    assert row.error_code == "cooldown"


async def test_resolve_during_debounce_cancels_pending(db_session, monkeypatch):
    enable_outbox(monkeypatch)
    row = await enqueue_watcher_incident(
        db_session,
        incident_id="incident-1",
        dedupe_key="monitor:pizero1",
        severity="warning",
        provider_id="uptimekuma",
        title="monitor down",
        task_id=None,
    )
    assert row is not None
    assert row.status == "pending"

    count = await cancel_pending_for_incident(
        db_session, provider_id="uptimekuma", dedupe_key="monitor:pizero1"
    )

    assert count == 1
    assert row.status == "cancelled"
    assert row.error_code == "resolved_during_debounce"


async def test_worker_records_telegram_message_id(db_session, monkeypatch):
    enable_outbox(monkeypatch)
    row = await enqueue(
        db_session,
        event_type="task.blocked",
        fingerprint_value=fingerprint("task", "blocked"),
        text="blocked",
        severity="critical",
        idempotency_suffix="delivery",
    )
    assert row is not None
    row_id = row.id
    await db_session.commit()

    async def fake_send(_chat_id, _text, _markup=None):
        return True, "telegram-42", ""

    monkeypatch.setattr("app.services.telegram_service.send_message_result", fake_send)

    assert await process_once() is True
    db_session.expire_all()
    delivered = await db_session.get(NotificationOutbox, row_id)
    assert delivered is not None
    assert delivered.status == "sent"
    assert delivered.telegram_message_id == "telegram-42"
    assert delivered.attempts == 1


async def test_cross_watcher_incidents_are_aggregated(db_session, monkeypatch):
    enable_outbox(monkeypatch)

    first = await enqueue_watcher_incident(
        db_session,
        incident_id="incident-vps",
        dedupe_key="vps-down",
        severity="critical",
        provider_id="vps",
        title="VPS unreachable",
        task_id=None,
        watcher_id="lab.alerts",
        group_key="topology:opnsense",
    )
    second = await enqueue_watcher_incident(
        db_session,
        incident_id="incident-kuma",
        dedupe_key="monitor-down",
        severity="critical",
        provider_id="uptimekuma",
        title="Monitor unreachable",
        task_id=None,
        watcher_id="uptimekuma.monitors",
        group_key="topology:opnsense",
    )

    assert first is second
    assert first is not None
    assert len(first.group_items) == 2
    assert first.reply_markup == {}
    assert len((await db_session.execute(select(NotificationOutbox))).scalars().all()) == 1

    cancelled = await cancel_pending_for_incident(
        db_session, provider_id="vps", dedupe_key="vps-down"
    )
    assert cancelled == 0
    assert first.status == "pending"


async def test_grouped_critical_shortens_warning_debounce(db_session, monkeypatch):
    enable_outbox(monkeypatch)
    monkeypatch.setenv("NOTIFICATION_WARNING_DEBOUNCE_SECONDS", "900")
    monkeypatch.setenv("NOTIFICATION_CRITICAL_DEBOUNCE_SECONDS", "120")
    get_settings.cache_clear()

    warning = await enqueue_watcher_incident(
        db_session,
        incident_id="warning-incident",
        dedupe_key="warning",
        severity="warning",
        provider_id="proxmox",
        title="Guest stopped",
        task_id=None,
        group_key="topology:opnsense",
    )
    assert warning is not None
    warning_available_at = warning.available_at
    before_critical = utcnow()
    critical = await enqueue_watcher_incident(
        db_session,
        incident_id="critical-incident",
        dedupe_key="critical",
        severity="critical",
        provider_id="mikrotik",
        title="Router unreachable",
        task_id=None,
        group_key="topology:opnsense",
    )

    assert critical is warning
    assert critical is not None
    assert critical.severity == "critical"
    assert "🔴" in critical.text
    assert "Router unreachable" in critical.text
    assert critical.available_at < warning_available_at
    assert critical.available_at <= before_critical + timedelta(seconds=121)


async def test_group_aggregation_deduplicates_incidents(db_session, monkeypatch):
    enable_outbox(monkeypatch)
    item = {
        "incident_id": "incident-vps",
        "watcher_id": "lab.alerts",
        "provider_id": "vps",
        "severity": "critical",
        "title": "VPS unreachable",
    }

    row = await enqueue(
        db_session,
        event_type="watcher.incident",
        fingerprint_value="v1:vps:down",
        text="VPS unreachable",
        severity="critical",
        group_key="topology:opnsense",
        group_items=[item, item],
    )
    assert row is not None
    await enqueue(
        db_session,
        event_type="watcher.incident",
        fingerprint_value="v1:vps:down-again",
        text="VPS unreachable",
        severity="critical",
        group_key="topology:opnsense",
        group_items=[item],
    )

    assert len(row.group_items) == 1


async def test_immediate_critical_bypasses_debounce(db_session, monkeypatch):
    enable_outbox(monkeypatch)
    monkeypatch.setenv("NOTIFICATION_CRITICAL_DEBOUNCE_SECONDS", "120")
    get_settings.cache_clear()

    row = await enqueue_watcher_incident(
        db_session,
        incident_id="disk-failure",
        dedupe_key="disk-failure",
        severity="critical",
        provider_id="proxmox",
        title="SMART failure",
        task_id=None,
        watcher_id="storage.disks",
        immediate=True,
    )
    assert row is not None

    assert row.available_at <= utcnow()


async def test_worker_edits_sent_group_when_auto_resolved(db_session, monkeypatch):
    enable_outbox(monkeypatch)
    row = NotificationOutbox(
        idempotency_key="resolved-group",
        fingerprint="v1:group:resolved",
        group_key="topology:opnsense",
        group_items=[],
        event_type="watcher.incident",
        severity="critical",
        status="resolve_pending",
        text="Internet outage",
        telegram_message_id="telegram-99",
        available_at=utcnow(),
    )
    db_session.add(row)
    await db_session.commit()
    row_id = row.id

    async def fake_edit(_chat_id, message_id, text):
        assert message_id == "telegram-99"
        assert "Resolved automatically" in text
        return True, ""

    monkeypatch.setattr("app.services.telegram_service.edit_message_result", fake_edit)

    assert await process_once() is True
    db_session.expire_all()
    updated = await db_session.get(NotificationOutbox, row_id)
    assert updated is not None
    assert updated.status == "sent"
    assert updated.error_code == "auto_resolved"
