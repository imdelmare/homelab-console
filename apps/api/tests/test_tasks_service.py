import pytest
from sqlalchemy import select

from app.db.models import AuditEvent, Incident, McpClient, ProviderConfiguration, TaskEvent, ToolInvocation, utcnow
from app.domain.actors import Actor
from app.services.tasks_service import (
    TaskServiceError,
    add_check,
    add_finding,
    add_note,
    claim_task,
    claim_task_as_operator,
    complete_task_as_operator,
    complete_check,
    complete_task,
    create_task,
    handoff_operator_task_to_client,
    list_checks,
    list_resolutions,
    list_tasks,
    recommended_next_step,
    release_task,
    reopen_task,
    resolve_finding,
    set_status,
    skip_check,
    task_context,
    update_summary,
)

OPERATOR = Actor(kind="user", id="operator", label="operator")
CLAUDE = Actor(kind="service", id="claude", label="Claude")
CODEX = Actor(kind="service", id="codex", label="Codex")
CLINE = Actor(kind="agent", id="cline", label="Cline")
OPENCODE = Actor(kind="agent", id="opencode", label="OpenCode")
FIXER = Actor(kind="agent", id="fixer", label="Fixer")


async def test_create_task_records_required_fields_events_and_audit(db_session):
    task = await create_task(db_session, "Diagnose NAS", "Find why SMB is down", OPERATOR)
    await db_session.commit()

    assert task.status == "open"
    assert task.created_by == "user:operator"
    assert task.source == "rest"
    assert task.version == 1
    assert task.last_activity_at is not None

    events = (await db_session.execute(
        select(TaskEvent).where(TaskEvent.task_id == task.id)
    )).scalars().all()
    assert [event.kind for event in events] == ["task.created"]

    audit = (await db_session.execute(
        select(AuditEvent).where(AuditEvent.task_id == task.id, AuditEvent.action == "task.created")
    )).scalars().all()
    assert len(audit) == 1


async def test_claim_is_idempotent_for_same_agent_and_rejects_other_agent(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()

    claimed = await claim_task(db_session, task.id, "agent:claude", CLAUDE)
    assert claimed.status == "claimed"
    assert claimed.assigned_agent == "agent:claude"
    version = claimed.version

    same = await claim_task(db_session, task.id, "agent:claude", CLAUDE)
    assert same.version == version

    with pytest.raises(TaskServiceError) as exc:
        await claim_task(db_session, task.id, "agent:codex", CODEX)
    assert exc.value.code == "task_already_claimed"

    status_events = (
        await db_session.execute(
            select(TaskEvent).where(
                TaskEvent.task_id == task.id,
                TaskEvent.kind == "task.status_changed",
            )
        )
    ).scalars().all()
    assert len(status_events) == 1
    assert status_events[0].payload == {
        "from": "open",
        "to": "claimed",
        "reason": "task_claimed",
        "automatic": False,
        "policy": "normal",
        "assigned_agent": "agent:claude",
    }


async def test_operator_claim_derives_owner_and_respects_version(db_session):
    task = await create_task(db_session, "manual task", "operator handles it", OPERATOR)
    await db_session.flush()

    with pytest.raises(TaskServiceError) as exc:
        await claim_task_as_operator(
            db_session,
            task.id,
            OPERATOR,
            expected_version=task.version + 1,
        )
    assert exc.value.code == "version_conflict"

    claimed = await claim_task_as_operator(
        db_session,
        task.id,
        OPERATOR,
        expected_version=task.version,
    )
    assert claimed.status == "claimed"
    assert claimed.assigned_agent == "user:operator"

    events = (await db_session.execute(
        select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.kind == "task.claimed")
    )).scalars().all()
    assert events[-1].payload == {"assigned_agent": "user:operator"}


async def test_agent_cannot_use_operator_claim(db_session):
    task = await create_task(db_session, "manual task", "operator handles it", OPERATOR)
    await db_session.flush()

    with pytest.raises(TaskServiceError) as exc:
        await claim_task_as_operator(db_session, task.id, CODEX)
    assert exc.value.code == "unauthorized"


async def test_operator_handoff_records_note_and_assigns_online_client_atomically(db_session):
    task = await create_task(db_session, "manual task", "handoff", OPERATOR)
    client = McpClient(
        agent_id="codex",
        client_label="workstation",
        host_fingerprint="host-1",
        token_hash="hash-operator-handoff",
        last_seen_at=utcnow(),
    )
    db_session.add(client)
    await db_session.flush()
    await claim_task_as_operator(db_session, task.id, OPERATOR, expected_version=task.version)

    handed_off = await handoff_operator_task_to_client(
        db_session,
        task.id,
        client.id,
        "Controllo manuale eseguito; verifica il servizio.",
        OPERATOR,
        expected_version=task.version,
    )

    assert handed_off.status == "claimed"
    assert handed_off.assigned_agent == "agent:codex"
    events = (await db_session.execute(
        select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.created_at)
    )).scalars().all()
    assert any(event.kind == "task.note_added" for event in events)
    handoff = next(event for event in events if event.kind == "task.operator_handoff")
    assert handoff.payload["from_user"] == "user:operator"
    assert handoff.payload["to_agent"] == "agent:codex"
    assert handoff.payload["client_id"] == client.id


async def test_operator_handoff_rejects_offline_client_without_changing_task(db_session):
    task = await create_task(db_session, "manual task", "handoff", OPERATOR)
    client = McpClient(
        agent_id="codex",
        client_label="offline",
        host_fingerprint="host-2",
        token_hash="hash-offline-handoff",
        last_seen_at=None,
    )
    db_session.add(client)
    await db_session.flush()
    await claim_task_as_operator(db_session, task.id, OPERATOR)
    version = task.version

    with pytest.raises(TaskServiceError) as exc:
        await handoff_operator_task_to_client(
            db_session, task.id, client.id, "handoff note", OPERATOR, expected_version=version
        )
    assert exc.value.code == "client_offline"
    assert task.assigned_agent == "user:operator"
    assert task.status == "claimed"
    assert task.version == version


async def test_operator_handoff_rejects_worker_registration_without_job(db_session):
    task = await create_task(db_session, "manual task", "worker handoff", OPERATOR)
    client = McpClient(
        agent_id="codex",
        client_label="remediation worker",
        host_fingerprint="worker-host",
        token_hash="hash-worker-handoff",
        last_seen_at=utcnow(),
        capabilities=["task-worker.v1"],
        principal_id="worker:11111111-1111-4111-8111-111111111111",
    )
    db_session.add(client)
    await db_session.flush()
    await claim_task_as_operator(db_session, task.id, OPERATOR)
    version = task.version

    with pytest.raises(TaskServiceError) as exc:
        await handoff_operator_task_to_client(
            db_session,
            task.id,
            client.id,
            "worker assignment must use a durable job",
            OPERATOR,
            expected_version=version,
        )
    assert exc.value.code == "worker_client_requires_assignment"
    assert task.assigned_agent == "user:operator"
    assert task.version == version


async def test_operator_completion_records_human_resolution(db_session):
    task = await create_task(db_session, "manual task", "complete", OPERATOR)
    await db_session.flush()
    await claim_task_as_operator(db_session, task.id, OPERATOR)

    completed = await complete_task_as_operator(
        db_session,
        task.id,
        "Risolto manualmente verificando il servizio.",
        OPERATOR,
        expected_version=task.version,
    )

    assert completed.status == "completed"
    assert completed.assigned_agent == ""
    events = (await db_session.execute(
        select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.created_at)
    )).scalars().all()
    operator_event = next(event for event in events if event.kind == "task.operator_completed")
    assert operator_event.payload["handled_by"] == "user:operator"
    assert operator_event.payload["note"] == "Risolto manualmente verificando il servizio."


async def test_cline_can_claim_task(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()

    claimed = await claim_task(db_session, task.id, "agent:cline", CLINE)
    assert claimed.status == "claimed"
    assert claimed.assigned_agent == "agent:cline"


async def test_opencode_can_claim_and_own_task(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()

    claimed = await claim_task(db_session, task.id, "agent:opencode", OPENCODE)
    assert claimed.status == "claimed"
    assert claimed.assigned_agent == "agent:opencode"

    investigating = await set_status(db_session, task.id, "investigating", OPENCODE)
    assert investigating.status == "investigating"

    with pytest.raises(TaskServiceError) as exc:
        await update_summary(db_session, task.id, "Codex must not mutate", CODEX)
    assert exc.value.code == "not_task_owner"


async def test_fixer_has_distinct_task_ownership(db_session):
    task = await create_task(db_session, "repair lab service", "operator dispatched", OPERATOR)
    await db_session.flush()

    claimed = await claim_task(db_session, task.id, "agent:fixer", OPERATOR)
    assert claimed.assigned_agent == "agent:fixer"

    investigating = await set_status(db_session, task.id, "investigating", FIXER)
    assert investigating.status == "investigating"

    with pytest.raises(TaskServiceError) as exc:
        await update_summary(db_session, task.id, "ordinary Claude must not mutate", CLAUDE)
    assert exc.value.code == "not_task_owner"


async def test_release_returns_claimed_task_to_open_and_respects_version(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, task.id, "agent:claude", CLAUDE)
    version = task.version

    with pytest.raises(TaskServiceError) as exc:
        await release_task(db_session, task.id, CLAUDE, expected_version=version - 1)
    assert exc.value.code == "version_conflict"

    released = await release_task(
        db_session,
        task.id,
        CLAUDE,
        expected_version=version,
        handoff_summary="Checked current state; ready for another agent.",
    )
    assert released.status == "open"
    assert released.assigned_agent == ""
    assert released.claimed_at is None
    status_events = (
        await db_session.execute(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id, TaskEvent.kind == "task.status_changed")
            .order_by(TaskEvent.created_at)
        )
    ).scalars().all()
    assert status_events[-1].payload == {
        "from": "claimed",
        "to": "open",
        "reason": "task_released",
        "automatic": False,
        "policy": "release",
        "from_agent": "agent:claude",
        "handoff_summary": "Checked current state; ready for another agent.",
    }


async def test_status_transitions_are_validated(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()

    with pytest.raises(TaskServiceError) as exc:
        await set_status(db_session, task.id, "completed", OPERATOR)
    assert exc.value.code == "invalid_transition"

    await set_status(db_session, task.id, "claimed", OPERATOR)
    await set_status(db_session, task.id, "investigating", OPERATOR)
    await set_status(db_session, task.id, "waiting_operator", OPERATOR)
    await set_status(db_session, task.id, "investigating", OPERATOR)
    await complete_task(db_session, task.id, OPERATOR, expected_version=task.version)
    assert task.status == "completed"
    assert task.completed_at is not None

    await reopen_task(db_session, task.id, OPERATOR, expected_version=task.version)
    assert task.status == "open"
    assert task.completed_at is None

    events = (await db_session.execute(
        select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.kind == "task.reopened")
    )).scalars().all()
    assert len(events) == 1
    assert events[0].payload == {"from_status": "completed"}


async def test_completing_a_task_captures_resolution_snapshot(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, task.id, "agent:claude", CLAUDE)
    db_session.add(
        Incident(
            dedupe_key="k",
            watcher_id="lab.alerts",
            status="open",
            severity="warning",
            provider_id="opnsense",
            title="gateway trouble",
            task_id=task.id,
        )
    )
    await db_session.flush()
    finding = await add_finding(
        db_session, task.id, "warning", "Gateway degraded", "latency high", CLAUDE, source="mcp"
    )
    await resolve_finding(db_session, task.id, finding.id, CLAUDE, source="mcp")
    await update_summary(db_session, task.id, "Fixed by restarting the WAN interface.", CLAUDE)
    await set_status(db_session, task.id, "investigating", CLAUDE, expected_version=task.version)

    await complete_task(db_session, task.id, CLAUDE, expected_version=task.version)

    resolutions = await list_resolutions(db_session, task.id)
    assert len(resolutions) == 1
    resolution = resolutions[0]
    assert resolution.summary == "Fixed by restarting the WAN interface."
    assert resolution.incident_id is not None
    assert resolution.resolved_findings == [
        {"id": finding.id, "title": "Gateway degraded", "severity": "warning"}
    ]
    assert resolution.resolved_by == "service:claude"
    assert resolution.source == "rest"


async def test_completing_task_without_incident_or_findings_still_captures_resolution(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await set_status(db_session, task.id, "claimed", OPERATOR)
    await set_status(db_session, task.id, "investigating", OPERATOR)

    await complete_task(db_session, task.id, OPERATOR, expected_version=task.version)

    resolutions = await list_resolutions(db_session, task.id)
    assert len(resolutions) == 1
    assert resolutions[0].incident_id is None
    assert resolutions[0].resolved_findings == []


async def test_reopen_and_recomplete_appends_a_second_resolution(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await set_status(db_session, task.id, "claimed", OPERATOR)
    await set_status(db_session, task.id, "investigating", OPERATOR)
    await complete_task(db_session, task.id, OPERATOR, expected_version=task.version)

    await reopen_task(db_session, task.id, OPERATOR, expected_version=task.version)
    await set_status(db_session, task.id, "claimed", OPERATOR, expected_version=task.version)
    await set_status(db_session, task.id, "investigating", OPERATOR, expected_version=task.version)
    await complete_task(db_session, task.id, OPERATOR, expected_version=task.version)

    resolutions = await list_resolutions(db_session, task.id)
    assert len(resolutions) == 2


async def test_summary_uses_optimistic_locking(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()

    await update_summary(db_session, task.id, "first", OPERATOR, expected_version=task.version)
    with pytest.raises(TaskServiceError) as exc:
        await update_summary(db_session, task.id, "stale", OPERATOR, expected_version=1)
    assert exc.value.code == "version_conflict"
    assert task.summary == "first"


async def test_findings_can_be_added_and_resolved_but_not_deleted(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, task.id, "agent:claude", CLAUDE)

    finding = await add_finding(
        db_session,
        task.id,
        "warning",
        "OPNsense degraded",
        "Gateway latency is above threshold",
        CLAUDE,
        source="mcp",
    )
    assert finding.created_by == "agent:claude"
    assert finding.resolved_at is None

    with pytest.raises(TaskServiceError) as exc:
        await resolve_finding(db_session, task.id, finding.id, CODEX, source="mcp")
    assert exc.value.code == "not_task_owner"

    await release_task(db_session, task.id, CLAUDE, handoff_summary="handoff")
    await claim_task(db_session, task.id, "agent:codex", CODEX)
    resolved = await resolve_finding(db_session, task.id, finding.id, CODEX, source="mcp")
    assert resolved.resolved_at is not None


async def test_finding_rejected_on_final_task_even_for_service_actor(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await set_status(db_session, task.id, "claimed", OPERATOR)
    await set_status(db_session, task.id, "investigating", OPERATOR)
    await complete_task(db_session, task.id, OPERATOR, expected_version=task.version)

    watcher = Actor(kind="service", id="watcher", label="Watcher")
    with pytest.raises(TaskServiceError) as exc:
        await add_finding(
            db_session,
            task.id,
            "warning",
            "still present",
            "must not mutate a completed task",
            watcher,
            source="watcher",
        )

    assert exc.value.code == "task_not_active"


@pytest.mark.parametrize("final_status", ["completed", "cancelled"])
async def test_all_task_mutations_are_rejected_after_finalization(db_session, final_status):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    finding = await add_finding(
        db_session, task.id, "warning", "still open", "evidence", OPERATOR
    )
    check = await add_check(db_session, task.id, "Verify state", OPERATOR)
    if final_status == "completed":
        await set_status(db_session, task.id, "claimed", OPERATOR)
        await set_status(db_session, task.id, "investigating", OPERATOR)
        await complete_task(db_session, task.id, OPERATOR, expected_version=task.version)
    else:
        await set_status(db_session, task.id, "cancelled", OPERATOR)

    operations = [
        lambda: update_summary(db_session, task.id, "late summary", OPERATOR),
        lambda: add_note(db_session, task.id, "late note", OPERATOR),
        lambda: add_finding(db_session, task.id, "warning", "late", "late", OPERATOR),
        lambda: resolve_finding(db_session, task.id, finding.id, OPERATOR),
        lambda: add_check(db_session, task.id, "Late check", OPERATOR),
        lambda: complete_check(db_session, task.id, check.id, OPERATOR),
        lambda: skip_check(db_session, task.id, check.id, OPERATOR, "late"),
    ]
    for operation in operations:
        with pytest.raises(TaskServiceError) as exc:
            await operation()
        assert exc.value.code == "task_not_active"


async def test_checks_pending_completed_and_skipped(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, task.id, "agent:claude", CLAUDE)

    ping = await add_check(db_session, task.id, "Ping gateway", CLAUDE, source="mcp")
    dns = await add_check(db_session, task.id, "Check DNS", CLAUDE, source="mcp")

    assert {check.id for check in await list_checks(db_session, task.id, status="pending")} == {
        ping.id,
        dns.id,
    }

    with pytest.raises(TaskServiceError) as exc:
        await complete_check(db_session, task.id, ping.id, CODEX, source="mcp")
    assert exc.value.code == "not_task_owner"

    await release_task(db_session, task.id, CLAUDE, handoff_summary="handoff")
    await claim_task(db_session, task.id, "agent:codex", CODEX)
    await complete_check(db_session, task.id, ping.id, CODEX, source="mcp")
    await skip_check(db_session, task.id, dns.id, CODEX, "not relevant after fix", source="mcp")

    completed = await list_checks(db_session, task.id, status="completed")
    skipped = await list_checks(db_session, task.id, status="skipped")
    assert [check.id for check in completed] == [ping.id]
    assert [check.id for check in skipped] == [dns.id]
    assert skipped[0].skip_reason == "not relevant after fix"


@pytest.mark.parametrize("status", ["claimed", "investigating", "waiting_operator", "blocked"])
async def test_release_works_from_every_active_status(db_session, status):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, task.id, "agent:claude", CLAUDE)
    if status in ("investigating", "waiting_operator", "blocked"):
        await set_status(db_session, task.id, "investigating", CLAUDE, expected_version=task.version)
    if status in ("waiting_operator", "blocked"):
        await set_status(db_session, task.id, status, CLAUDE, expected_version=task.version)

    released = await release_task(db_session, task.id, CLAUDE, handoff_summary="handoff")
    assert released.status == "open"
    assert released.assigned_agent == ""
    assert released.claimed_at is None

    events = (await db_session.execute(
        select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.kind == "task.released")
    )).scalars().all()
    assert events[0].payload == {
        "from_status": status,
        "from_agent": "agent:claude",
        "handoff_summary": "handoff",
    }


async def test_agent_release_requires_handoff_summary(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, task.id, "agent:claude", CLAUDE)

    with pytest.raises(TaskServiceError) as exc:
        await release_task(db_session, task.id, CLAUDE)
    assert exc.value.code == "invalid_input"


async def test_release_rejects_wrong_owner(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, task.id, "agent:claude", CLAUDE)

    with pytest.raises(TaskServiceError) as exc:
        await release_task(db_session, task.id, CODEX)
    assert exc.value.code == "not_task_owner"


async def test_agent_cannot_mutate_open_task_without_claiming(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()

    with pytest.raises(TaskServiceError) as exc:
        await update_summary(db_session, task.id, "hello", CLAUDE)
    assert exc.value.code == "not_task_owner"


async def test_operator_can_override_ownership(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, task.id, "agent:claude", CLAUDE)

    released = await release_task(db_session, task.id, OPERATOR)
    assert released.status == "open"


async def test_finding_can_link_invocation_of_the_same_task(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, task.id, "agent:claude", CLAUDE)

    invocation = ToolInvocation(
        task_id=task.id, actor_kind="agent", actor_id="claude", tool_id="lab.overview",
    )
    db_session.add(invocation)
    await db_session.flush()

    finding = await add_finding(
        db_session, task.id, "warning", "t", "d", CLAUDE, tool_invocation_id=invocation.id,
    )
    assert finding.tool_invocation_id == invocation.id


async def test_finding_rejects_invocation_of_another_task(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    other_task = await create_task(db_session, "other", "g", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, task.id, "agent:claude", CLAUDE)

    other_invocation = ToolInvocation(
        task_id=other_task.id, actor_kind="agent", actor_id="claude", tool_id="lab.overview",
    )
    db_session.add(other_invocation)
    await db_session.flush()

    with pytest.raises(TaskServiceError) as exc:
        await add_finding(
            db_session, task.id, "warning", "t", "d", CLAUDE, tool_invocation_id=other_invocation.id,
        )
    assert exc.value.code == "invalid_input"


@pytest.mark.parametrize(
    "status,pending,critical,expected",
    [
        ("open", False, False, "Claim the task before working on it."),
        ("claimed", False, False,
         "Set the task to investigating and begin with the relevant summary tool."),
        ("investigating", True, True, "Continue with the oldest pending check."),
        ("investigating", False, True, "Investigate the unresolved critical findings."),
        ("investigating", False, False,
         "Update the summary and complete the task when the goal is met."),
        ("waiting_operator", False, False, "Wait for or request operator input."),
        ("blocked", False, False, "Document the blocker or release the task for handoff."),
        ("completed", False, False, "No further action is required unless the task is reopened."),
        ("cancelled", False, False, ""),
    ],
)
def test_recommended_next_step_rules(status, pending, critical, expected):
    assert recommended_next_step(
        status, has_pending_checks=pending, has_open_critical_findings=critical
    ) == expected


async def test_task_context_is_compact_and_recommends_next_step(db_session):
    task = await create_task(db_session, "t", "g", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, task.id, "agent:claude", CLAUDE)
    await set_status(db_session, task.id, "investigating", CLAUDE, expected_version=task.version)
    await add_check(db_session, task.id, "Ping gateway", CLAUDE)
    db_session.add(
        ProviderConfiguration(
            id="opnsense",
            display_name="OPNsense",
            last_status="degraded",
        )
    )
    db_session.add(
        Incident(
            dedupe_key="incident-key",
            watcher_id="lab.alerts",
            status="open",
            severity="critical",
            provider_id="opnsense",
            title="Gateway latency high",
            description="Gateway latency is above threshold",
            task_id=task.id,
            payload={"token": "secret", "safe": "value"},
        )
    )
    await db_session.flush()

    context = await task_context(db_session, task.id)
    assert context["recommended_next_step"] == "Continue with the oldest pending check."
    assert context["incident"]["type"] == "gateway_alert"
    assert context["incident"]["payload"]["token"] == "[REDACTED]"
    assert context["provider_ids"] == ["opnsense"]
    assert context["provider_states"][0]["status"] == "degraded"
    assert context["budget"] == {"max_tool_calls": 4, "max_minutes": 10}
    assert [tool["tool_id"] for tool in context["recommended_tools"]][:2] == [
        "opnsense.summary",
        "opnsense.gateways.status",
    ]
    assert len(context["pending_checks"]) == 1
    assert set(context.keys()) == {
        "task", "incident", "provider_ids", "provider_states", "brief", "recent_findings",
        "pending_checks", "completed_checks", "recent_tool_invocations", "recent_events",
        "recent_watcher_runs", "recommended_tools", "budget", "stop_conditions",
        "recommended_next_step",
    }


async def test_list_tasks_filters_status_and_assigned_agent(db_session):
    first = await create_task(db_session, "first", "g", OPERATOR)
    second = await create_task(db_session, "second", "g", OPERATOR)
    await db_session.flush()
    await claim_task(db_session, second.id, "agent:codex", CODEX)

    open_tasks = await list_tasks(db_session, status="open")
    codex_tasks = await list_tasks(db_session, assigned_agent="agent:codex")

    assert [task.id for task in open_tasks] == [first.id]
    assert [task.id for task in codex_tasks] == [second.id]
