from datetime import timedelta

from tests.conftest import do_login
from app.db.models import McpClient, McpPairingRequest, TaskEvent, utcnow
from app.providers.base import ProviderHealth
from app.services import inventory
from sqlalchemy import select


async def test_rest_task_lifecycle_endpoints(client, user, capture_adapter):
    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}

    created = await client.post(
        "/api/tasks",
        json={"title": "router issue", "goal": "collect evidence"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    task = created.json()

    claimed = await client.post(
        f"/api/tasks/{task['id']}/claim",
        json={"agent_id": "agent:codex"},
        headers=headers,
    )
    assert claimed.status_code == 200, claimed.text
    task = claimed.json()
    assert task["status"] == "claimed"
    assert task["assigned_agent"] == "agent:codex"

    summary = await client.patch(
        f"/api/tasks/{task['id']}/summary",
        json={"summary": "Gateway check started", "expected_version": task["version"]},
        headers=headers,
    )
    assert summary.status_code == 200, summary.text
    task = summary.json()
    assert task["summary"] == "Gateway check started"

    finding = await client.post(
        f"/api/tasks/{task['id']}/findings",
        json={
            "severity": "warning",
            "title": "Latency high",
            "description": "Gateway latency is above threshold",
        },
        headers=headers,
    )
    assert finding.status_code == 201, finding.text
    assert finding.json()["severity"] == "warning"

    check = await client.post(
        f"/api/tasks/{task['id']}/checks",
        json={"description": "Verify DNS"},
        headers=headers,
    )
    assert check.status_code == 201, check.text

    context = await client.get(f"/api/tasks/{task['id']}/context")
    assert context.status_code == 200, context.text
    assert context.json()["budget"]["max_tool_calls"] == 4
    assert "recommended_tools" in context.json()

    completed_check = await client.post(
        f"/api/tasks/{task['id']}/checks/{check.json()['id']}/complete",
        headers=headers,
    )
    assert completed_check.status_code == 200, completed_check.text
    assert completed_check.json()["status"] == "completed"

    status = await client.post(
        f"/api/tasks/{task['id']}/status",
        json={"status": "investigating"},
        headers=headers,
    )
    assert status.status_code == 200, status.text
    task = status.json()

    completed = await client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"expected_version": task["version"]},
        headers=headers,
    )
    assert completed.status_code == 200, completed.text
    task = completed.json()
    assert task["status"] == "completed"

    reopened = await client.post(
        f"/api/tasks/{task['id']}/reopen",
        json={"expected_version": task["version"]},
        headers=headers,
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "open"


async def test_operator_can_claim_self_without_supplying_identity(client, user, capture_adapter):
    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}
    created = await client.post(
        "/api/tasks",
        json={"title": "manual task", "goal": "handle from the console"},
        headers=headers,
    )
    task = created.json()

    claimed = await client.post(
        f"/api/tasks/{task['id']}/claim-self",
        json={"expected_version": task["version"]},
        headers=headers,
    )

    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "claimed"
    assert claimed.json()["assigned_agent"] == f"user:{user.username}"


async def test_operator_can_close_owned_task_as_human(client, user, capture_adapter):
    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}
    created = (await client.post(
        "/api/tasks",
        json={"title": "manual resolution", "goal": "close with evidence"},
        headers=headers,
    )).json()
    claimed = (await client.post(
        f"/api/tasks/{created['id']}/claim-self",
        json={"expected_version": created["version"]},
        headers=headers,
    )).json()

    completed = await client.post(
        f"/api/tasks/{created['id']}/complete-as-operator",
        json={"expected_version": claimed["version"], "note": "Risolta manualmente."},
        headers=headers,
    )

    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"
    assert completed.json()["resolution_label"] == "human_handled"
    detail = await client.get(f"/api/tasks/{created['id']}")
    assert detail.json()["resolution_label"] == "human_handled"
    assert any(event["kind"] == "task.operator_completed" for event in detail.json()["events"])


async def test_operator_handoff_endpoint_targets_online_mcp_client(
    client, user, capture_adapter, db_session
):
    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}
    mcp_client = McpClient(
        agent_id="codex",
        client_label="operator workstation",
        host_fingerprint="operator-host",
        token_hash="api-operator-handoff-hash",
        last_seen_at=utcnow(),
    )
    db_session.add(mcp_client)
    await db_session.commit()
    created = (await client.post(
        "/api/tasks",
        json={"title": "manual handoff", "goal": "pass context"},
        headers=headers,
    )).json()
    claimed = (await client.post(
        f"/api/tasks/{created['id']}/claim-self",
        json={"expected_version": created["version"]},
        headers=headers,
    )).json()

    handed_off = await client.post(
        f"/api/tasks/{created['id']}/handoff-to-client",
        json={
            "expected_version": claimed["version"],
            "client_id": mcp_client.id,
            "note": "Verifica finale richiesta a Codex.",
        },
        headers=headers,
    )

    assert handed_off.status_code == 200, handed_off.text
    assert handed_off.json()["status"] == "claimed"
    assert handed_off.json()["assigned_agent"] == "agent:codex"


async def test_create_task_endpoint_enqueues_task_router(client, user, capture_adapter, db_session, monkeypatch):
    from app.core.settings import get_settings
    from app.db.models import TaskRouterJob

    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}
    monkeypatch.setattr(get_settings(), "task_router_enabled", True)

    response = await client.post(
        "/api/tasks",
        json={"title": "manual check", "goal": "collect evidence"},
        headers=headers,
    )

    assert response.status_code == 201, response.text
    job = await db_session.scalar(
        select(TaskRouterJob).where(TaskRouterJob.task_id == response.json()["id"])
    )
    assert job is not None
    assert job.status == "pending"
    assert job.actor_id == "operator"
    assert job.context == {
        "trigger": "manual",
        "title": "manual check",
        "goal": "collect evidence",
    }


async def test_task_list_includes_router_status(client, user, capture_adapter, db_session, monkeypatch):
    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}

    created = await client.post(
        "/api/tasks",
        json={"title": "router badge", "goal": "show route state"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    db_session.add(TaskEvent(task_id=task_id, kind="task.router_decision", payload={"decision": {}}))
    await db_session.commit()

    response = await client.get("/api/tasks")

    assert response.status_code == 200, response.text
    row = next(item for item in response.json() if item["id"] == task_id)
    assert row["router_status"] == "routed"


async def test_rest_rejects_invalid_task_transition(client, user, capture_adapter):
    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}

    created = await client.post(
        "/api/tasks",
        json={"title": "router issue", "goal": "collect evidence"},
        headers=headers,
    )
    task = created.json()

    completed = await client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"expected_version": task["version"]},
        headers=headers,
    )
    assert completed.status_code == 409
    assert completed.json()["detail"]["code"] == "invalid_transition"


async def test_rest_lists_dependency_graph(client, user, capture_adapter, monkeypatch):
    await do_login(client, capture_adapter)
    entries = [
        inventory.DependencyEntry(id="opnsense", label="Gateway", depends_on=[]),
        inventory.DependencyEntry(id="homeassistant", label="Home Assistant", depends_on=["opnsense"]),
    ]
    monkeypatch.setattr("app.api.routes_control.list_dependencies", lambda: entries)

    response = await client.get("/api/inventory/dependencies")

    assert response.status_code == 200, response.text
    assert response.json() == [
        {"id": "opnsense", "label": "Gateway", "kind": "provider", "depends_on": []},
        {
            "id": "homeassistant",
            "label": "Home Assistant",
            "kind": "provider",
            "depends_on": ["opnsense"],
        },
    ]


async def test_create_provider_task_endpoint(client, user, capture_adapter, db_session, monkeypatch):
    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}

    async def fake_snapshot(_db):
        return [
            ProviderHealth(
                provider_id="opnsense",
                status="degraded",
                detail="gateway latency high",
            )
        ]

    monkeypatch.setattr("app.api.routes_control.provider_health_snapshot", fake_snapshot)
    routed = []

    async def fake_enqueue_task_routing(_db, task, _actor, *, source, context=None):
        assert context is not None
        routed.append((task.source, source, context["provider_id"]))

    monkeypatch.setattr("app.api.routes_control.enqueue_task_routing", fake_enqueue_task_routing)

    response = await client.post("/api/providers/opnsense/task", json={}, headers=headers)

    assert response.status_code == 201, response.text
    task = response.json()
    assert task["source"] == "provider"
    assert "Provider opnsense is degraded" in task["goal"]
    assert "gateway latency high" in task["goal"]
    assert "gateway_alert / connectivity_alert" in task["goal"]

    events = (
        await db_session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task["id"], TaskEvent.kind == "task.provider_context")
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["provider_id"] == "opnsense"
    assert events[0].payload["status"] == "degraded"
    assert routed == [("provider", "provider", "opnsense")]


async def test_rest_lists_and_revokes_mcp_clients(client, user, capture_adapter, db_session):
    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}
    db_session.add(
        McpClient(
            agent_id="codex",
            client_label="Codex workstation",
            host_fingerprint="host-a",
            token_hash="hash",
            token_hint="abcd1234",
        )
    )
    await db_session.commit()

    listed = await client.get("/api/mcp/clients")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body[0]["agent_id"] == "codex"
    assert body[0]["token_hint"] == "abcd1234"
    assert body[0]["capabilities"] == []
    assert body[0]["principal_id"] == "agent:codex"
    assert "token_hash" not in body[0]

    missing_capability_csrf = await client.put(
        f"/api/mcp/clients/{body[0]['id']}/capabilities",
        json={"capabilities": ["task-worker.v1"]},
    )
    assert missing_capability_csrf.status_code == 403

    unconfirmed = await client.put(
        f"/api/mcp/clients/{body[0]['id']}/capabilities",
        json={"capabilities": ["task-worker.v1"]},
        headers=headers,
    )
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["detail"]["code"] == "worker_conversion_confirmation_required"

    capability = await client.put(
        f"/api/mcp/clients/{body[0]['id']}/capabilities",
        json={"capabilities": ["task-worker.v1"], "confirm_worker_conversion": True},
        headers=headers,
    )
    assert capability.status_code == 200, capability.text
    assert capability.json()["capabilities"] == ["task-worker.v1"]
    assert capability.json()["principal_id"] == f"agent:worker:{body[0]['id']}"

    revoked = await client.post(
        f"/api/mcp/clients/{body[0]['id']}/revoke",
        json={"reason": "operator test"},
        headers=headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] is not None
    assert revoked.json()["revoked_reason"] == "operator test"

    missing_csrf = await client.delete(f"/api/mcp/clients/{body[0]['id']}")
    assert missing_csrf.status_code == 403, missing_csrf.text

    forgotten = await client.delete(
        f"/api/mcp/clients/{body[0]['id']}",
        headers=headers,
    )
    assert forgotten.status_code == 204, forgotten.text
    assert await db_session.get(McpClient, body[0]["id"]) is None


async def test_rest_cannot_forget_active_mcp_client(client, user, capture_adapter, db_session):
    _, csrf = await do_login(client, capture_adapter)
    active = McpClient(
        agent_id="codex",
        client_label="Active Codex",
        host_fingerprint="active-host",
        token_hash="active-hash",
        token_hint="active",
    )
    db_session.add(active)
    await db_session.commit()

    response = await client.delete(
        f"/api/mcp/clients/{active.id}",
        headers={"x-csrf-token": csrf},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "client_not_revoked"
    assert await db_session.get(McpClient, active.id) is not None


async def test_public_mcp_pairing_onboarding(client, db_session, monkeypatch):
    captured = {}

    async def fake_send(_request, nonce):
        captured["nonce"] = nonce
        return "sent"

    monkeypatch.setattr("app.services.mcp_clients._send_pairing_telegram", fake_send)

    started = await client.post(
        "/api/mcp/pairing/start",
        json={
            "agent_id": "cline",
            "client_label": "Cline laptop",
            "host_fingerprint": "cline-host",
        },
    )
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["delivery_status"] == "sent"
    assert body["pairing_secret"]
    assert captured["nonce"].isdigit()

    pending = await client.post(
        "/api/mcp/pairing/consume",
        json={"request_id": body["request_id"], "pairing_secret": body["pairing_secret"]},
    )
    assert pending.status_code == 202, pending.text
    assert pending.json()["error"]["code"] == "pairing_not_approved"

    request = await db_session.get(McpPairingRequest, body["request_id"])
    request.status = "approved"
    request.decided_by = "telegram:111"
    await db_session.commit()

    consumed = await client.post(
        "/api/mcp/pairing/consume",
        json={"request_id": body["request_id"], "pairing_secret": body["pairing_secret"]},
    )
    assert consumed.status_code == 200, consumed.text
    result = consumed.json()
    assert result["ok"] is True
    assert result["token"].startswith("hmc_")
    assert result["client"]["agent_id"] == "cline"
    assert result["client"]["client_label"] == "Cline laptop"


async def test_mcp_pairing_history_is_authenticated_and_redacted(client, user, capture_adapter, db_session):
    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}
    db_session.add(
        McpPairingRequest(
            agent_id="codex",
            client_label="Codex desk",
            host_fingerprint="desk",
            status="denied",
            pairing_secret_hash="secret-hash",
            approve_nonce_hash="nonce-hash",
            expires_at=utcnow() + timedelta(minutes=5),
            decided_by="telegram:111",
            delivery_status="sent",
        )
    )
    await db_session.commit()

    response = await client.get("/api/mcp/pairing/requests", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body[0]["agent_id"] == "codex"
    assert body[0]["status"] == "denied"
    assert body[0]["decided_by"] == "telegram:111"
    assert "pairing_secret_hash" not in body[0]
    assert "approve_nonce_hash" not in body[0]


async def test_rest_assign_worker_requires_capability_and_claims_task(client, user, capture_adapter, db_session):
    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}
    worker = McpClient(
        agent_id="codex", client_label="worker", host_fingerprint="worker",
        token_hash="worker-hash", token_hint="worker",
    )
    db_session.add(worker)
    await db_session.commit()
    created = await client.post("/api/tasks", json={"title": "repair", "goal": "fix"}, headers=headers)
    task = created.json()

    denied = await client.post(
        f"/api/tasks/{task['id']}/assign-worker",
        json={"client_id": worker.id, "expected_version": task["version"]}, headers=headers,
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "worker_capability_required"

    worker.capabilities = ["task-worker.v1"]
    worker.principal_id = f"worker:{worker.id}"
    await db_session.commit()
    assigned = await client.post(
        f"/api/tasks/{task['id']}/assign-worker",
        json={"client_id": worker.id, "expected_version": task["version"]}, headers=headers,
    )
    assert assigned.status_code == 201, assigned.text
    assert assigned.json()["task"]["assigned_agent"] == f"agent:worker:{worker.id}"
    assert assigned.json()["job"]["client_id"] == worker.id
