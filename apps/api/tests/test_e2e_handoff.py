"""End-to-end test of the Claude <-> Codex task handoff over MCP, plus the
REST surface a human operator uses. No LLM is involved: "claude" and
"codex" here are just two MCP client identities (MCP_AGENT_ID), driving the
same tool calls a real agent would issue.
"""

import json

from sqlalchemy import select

from app.db.models import Task, ToolInvocation
from tests.conftest import as_mcp_agent, do_login, load_mcp_server_module


async def _mcp(server, name: str, args: dict) -> dict:
    content = await server.handle_call_tool(name, args)
    return json.loads(content[0].text)


async def test_full_handoff_scenario(client, user, capture_adapter, db_session, monkeypatch):
    server = load_mcp_server_module()

    # 1. User creates a task via REST.
    _, csrf = await do_login(client, capture_adapter)
    headers = {"x-csrf-token": csrf}
    created = await client.post(
        "/api/tasks", json={"title": "Investigate NAS outage", "goal": "Find why SMB is down"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    assert created.json()["assigned_agent"] == ""

    # 2. Claude claims the task.
    await as_mcp_agent(server, monkeypatch, "claude")
    claimed = await _mcp(server, "tasks_claim", {"task_id": task_id})
    assert claimed["result"]["assigned_agent"] == "agent:claude"
    version = claimed["result"]["version"]

    # 3. Claude sets investigating.
    status = await _mcp(
        server, "tasks_set_status",
        {"task_id": task_id, "status": "investigating", "expected_version": version},
    )
    assert status["result"]["status"] == "investigating"
    version = status["result"]["version"]

    # 4. Claude runs lab.overview with task_id.
    overview = await _mcp(server, "lab_overview", {"task_id": task_id})
    assert overview["ok"] is True
    invocation_id = overview["invocation_id"]

    # 5. Invocation is linked to the task.
    invocation = await db_session.get(ToolInvocation, invocation_id)
    assert invocation is not None
    assert invocation.task_id == task_id

    task_row = await db_session.get(Task, task_id)
    version = task_row.version

    # 6. Claude adds a finding referencing that invocation.
    finding = await _mcp(
        server, "tasks_add_finding",
        {
            "task_id": task_id, "severity": "warning", "title": "SMB down",
            "description": "Samba service is not responding",
            "tool_invocation_id": invocation_id,
        },
    )
    assert finding["result"]["tool_invocation_id"] == invocation_id

    # 7. Claude adds a pending check.
    check = await _mcp(server, "tasks_add_check", {"task_id": task_id, "description": "Restart smbd"})
    check_id = check["result"]["id"]

    # 8. Claude updates the summary.
    await db_session.refresh(task_row)
    summary = await _mcp(
        server, "tasks_update_summary",
        {"task_id": task_id, "summary": "SMB down, restart pending", "expected_version": task_row.version},
    )
    assert summary["result"]["summary"] == "SMB down, restart pending"
    claude_version = summary["result"]["version"]

    # 9. Codex cannot modify while Claude owns the task.
    await as_mcp_agent(server, monkeypatch, "codex")
    denied = await _mcp(server, "tasks_update_summary", {"task_id": task_id, "summary": "hijack"})
    assert denied["ok"] is False
    assert denied["error"]["code"] == "not_task_owner"

    # 10. Claude releases the task.
    await as_mcp_agent(server, monkeypatch, "claude")
    released = await _mcp(
        server,
        "tasks_release",
        {
            "task_id": task_id,
            "expected_version": claude_version,
            "handoff_summary": "SMB issue reproduced; Codex should verify restart path.",
        },
    )
    assert released["result"]["status"] == "open"
    assert released["result"]["assigned_agent"] == ""

    # 11. Codex claims the task.
    await as_mcp_agent(server, monkeypatch, "codex")
    codex_claim = await _mcp(server, "tasks_claim", {"task_id": task_id})
    assert codex_claim["result"]["assigned_agent"] == "agent:codex"

    # 12. Codex reads tasks.context. Release always returns the task to
    # "open" (never resumes the previous in-progress status), so the newly
    # claimed task starts in "claimed" again.
    context = await _mcp(server, "tasks_context", {"task_id": task_id})
    ctx = context["result"]
    assert ctx["task"]["summary"] == "SMB down, restart pending"
    assert len(ctx["pending_checks"]) == 1
    assert ctx["recommended_next_step"] == (
        "Set the task to investigating and begin with the relevant summary tool."
    )

    # 13. Codex sets investigating and completes the pending check.
    task_row = await db_session.get(Task, task_id)
    await db_session.refresh(task_row)
    await _mcp(
        server, "tasks_set_status",
        {"task_id": task_id, "status": "investigating", "expected_version": task_row.version},
    )
    completed_check = await _mcp(server, "tasks_complete_check", {"task_id": task_id, "check_id": check_id})
    assert completed_check["result"]["status"] == "completed"

    # 14. Codex updates the summary.
    task_row = await db_session.get(Task, task_id)
    await db_session.refresh(task_row)
    codex_summary = await _mcp(
        server, "tasks_update_summary",
        {"task_id": task_id, "summary": "smbd restarted, SMB back up", "expected_version": task_row.version},
    )
    version = codex_summary["result"]["version"]

    # 15. Codex completes the task (only reachable from investigating,
    # already set in step 13).
    complete = await _mcp(
        server, "tasks_complete", {"task_id": task_id, "expected_version": version}
    )
    assert complete["result"]["status"] == "completed"
    assert complete["result"]["completed_at"] is not None

    # 16. Task is visible as completed with full history via REST.
    detail = await client.get(f"/api/tasks/{task_id}", headers=headers)
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["status"] == "completed"
    kinds = [event["kind"] for event in detail_body["events"]]
    assert "task.created" in kinds
    assert "task.claimed" in kinds
    assert "task.tool_invoked" in kinds
    assert "task.finding_added" in kinds
    assert "task.check_added" in kinds
    assert "task.check_completed" in kinds
    assert "task.released" in kinds
    assert kinds.count("task.claimed") == 2  # claude, then codex
    assert len(detail_body["findings"]) == 1
    assert len(detail_body["checks"]) == 1

    # 17. User reopens the task.
    reopened = await client.post(
        f"/api/tasks/{task_id}/reopen",
        json={"expected_version": complete["result"]["version"]},
        headers=headers,
    )
    assert reopened.status_code == 200, reopened.text
    reopened_body = reopened.json()
    assert reopened_body["status"] == "open"
    assert reopened_body["assigned_agent"] == ""
    assert reopened_body["completed_at"] is None

    detail_after_reopen = await client.get(f"/api/tasks/{task_id}", headers=headers)
    kinds_after = [event["kind"] for event in detail_after_reopen.json()["events"]]
    assert "task.reopened" in kinds_after


async def test_concurrent_claim_via_mcp_rejects_second_agent(client, user, capture_adapter, monkeypatch):
    """Two agents racing tasks.claim on the same open task: only the first
    wins, driven through the real MCP transport both agents use."""
    server = load_mcp_server_module()
    _, csrf = await do_login(client, capture_adapter)
    created = await client.post(
        "/api/tasks", json={"title": "race", "goal": "g"}, headers={"x-csrf-token": csrf}
    )
    task_id = created.json()["id"]

    await as_mcp_agent(server, monkeypatch, "claude")
    first = await _mcp(server, "tasks_claim", {"task_id": task_id})
    assert first["result"]["assigned_agent"] == "agent:claude"

    await as_mcp_agent(server, monkeypatch, "codex")
    second = await _mcp(server, "tasks_claim", {"task_id": task_id})
    assert second["ok"] is False
    assert second["error"]["code"] == "task_already_claimed"


async def test_version_conflict_via_mcp(client, user, capture_adapter, monkeypatch):
    server = load_mcp_server_module()
    _, csrf = await do_login(client, capture_adapter)
    created = await client.post(
        "/api/tasks", json={"title": "t", "goal": "g"}, headers={"x-csrf-token": csrf}
    )
    task_id = created.json()["id"]

    await as_mcp_agent(server, monkeypatch, "claude")
    await _mcp(server, "tasks_claim", {"task_id": task_id})
    stale = await _mcp(
        server, "tasks_update_summary",
        {"task_id": task_id, "summary": "stale write", "expected_version": 1},
    )
    assert stale["ok"] is False
    assert stale["error"]["code"] == "version_conflict"


async def test_input_limits_are_enforced(client, user, capture_adapter):
    """REST rejects an oversized title before it ever reaches the service
    layer (Pydantic Field(max_length=...) on the request model)."""
    _, csrf = await do_login(client, capture_adapter)
    response = await client.post(
        "/api/tasks", json={"title": "x" * 257, "goal": ""}, headers={"x-csrf-token": csrf}
    )
    assert response.status_code == 422
