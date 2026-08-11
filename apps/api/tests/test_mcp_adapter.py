from contextlib import asynccontextmanager

from tests.conftest import load_mcp_server_module


async def test_http_bearer_validation(monkeypatch):
    server = load_mcp_server_module()
    monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", "http-token")
    from app.core.settings import get_settings

    get_settings.cache_clear()
    assert await server.check_http_bearer_token(None) is False
    assert await server.check_http_bearer_token("Basic http-token") is False
    assert await server.check_http_bearer_token("Bearer wrong") is False
    assert await server.check_http_bearer_token("Bearer http-token") is False


async def test_http_bearer_accepts_registered_client_token(db_session, monkeypatch):
    server = load_mcp_server_module()
    monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", "")
    monkeypatch.setenv("MCP_AGENT_ID", "codex")
    from app.core.settings import get_settings
    from app.domain.actors import Actor
    from app.services.mcp_clients import create_client_token

    get_settings.cache_clear()
    issued = await create_client_token(
        db_session,
        agent_id="codex",
        client_label="Codex remote",
        host_fingerprint="remote",
        actor=Actor(kind="telegram", id="111"),
    )
    await db_session.commit()

    assert await server.check_http_bearer_token(f"Bearer {issued.token}") is True
    assert await server.check_http_bearer_token("Bearer wrong") is False


async def test_http_bearer_uses_registered_client_actor(db_session, monkeypatch):
    server = load_mcp_server_module()
    monkeypatch.setenv("MCP_HTTP_BEARER_TOKEN", "")
    from app.core.settings import get_settings
    from app.domain.actors import Actor
    from app.services.mcp_clients import create_client_token

    get_settings.cache_clear()
    issued = await create_client_token(
        db_session,
        agent_id="cline",
        client_label="Cline workstation",
        host_fingerprint="cline-host",
        actor=Actor(kind="telegram", id="111"),
    )
    await db_session.commit()

    client = await server.authenticate_http_bearer_token(f"Bearer {issued.token}")
    assert client is not None
    token = server._http_actor.set(Actor(kind="agent", id=client.agent_id, label=client.client_label))
    try:
        actor = server.get_mcp_actor()
    finally:
        server._http_actor.reset(token)
    assert actor.id == "cline"
    assert actor.label == "Cline workstation"


async def test_http_bearer_uses_fixer_actor(db_session, monkeypatch):
    server = load_mcp_server_module()
    from app.domain.actors import Actor
    from app.services.mcp_clients import create_client_token

    issued = await create_client_token(
        db_session,
        agent_id="fixer",
        client_label="Fixer",
        host_fingerprint="claude",
        actor=Actor(kind="telegram", id="111"),
    )
    await db_session.commit()

    client = await server.authenticate_http_bearer_token(f"Bearer {issued.token}")
    assert client is not None
    token = server._http_actor.set(Actor(kind="agent", id=client.agent_id, label=client.client_label))
    try:
        actor = server.get_mcp_actor()
    finally:
        server._http_actor.reset(token)
    assert actor.id == "fixer"
    assert actor.label == "Fixer"


async def test_http_bearer_uses_opencode_actor(db_session):
    server = load_mcp_server_module()
    from app.domain.actors import Actor
    from app.services.mcp_clients import create_client_token

    issued = await create_client_token(
        db_session,
        agent_id="opencode",
        client_label="OpenCode workstation",
        host_fingerprint="opencode-host",
        actor=Actor(kind="telegram", id="111"),
    )
    await db_session.commit()

    client = await server.authenticate_http_bearer_token(f"Bearer {issued.token}")
    assert client is not None
    token = server._http_actor.set(Actor(kind="agent", id=client.agent_id, label=client.client_label))
    try:
        actor = server.get_mcp_actor()
    finally:
        server._http_actor.reset(token)
    assert actor.id == "opencode"
    assert actor.label == "OpenCode workstation"


async def test_worker_capability_uses_client_specific_actor(db_session):
    server = load_mcp_server_module()
    from app.domain.actors import Actor
    from app.services.mcp_clients import create_client_token, set_mcp_client_capabilities

    first = await create_client_token(
        db_session,
        agent_id="codex",
        client_label="Codex worker one",
        host_fingerprint="worker-one",
        actor=Actor(kind="telegram", id="111"),
    )
    second = await create_client_token(
        db_session,
        agent_id="codex",
        client_label="Codex worker two",
        host_fingerprint="worker-two",
        actor=Actor(kind="telegram", id="111"),
    )
    await set_mcp_client_capabilities(
        db_session,
        client_id=first.client.id,
        capabilities=["task-worker.v1"],
        actor=Actor(kind="telegram", id="111"),
        confirm_worker_conversion=True,
    )
    await set_mcp_client_capabilities(
        db_session,
        client_id=second.client.id,
        capabilities=["task-worker.v1"],
        actor=Actor(kind="telegram", id="111"),
        confirm_worker_conversion=True,
    )
    await db_session.commit()

    actors = []
    for issued in (first, second):
        client = await server.authenticate_http_bearer_token(f"Bearer {issued.token}")
        token = server._mcp_client.set(client)
        try:
            actors.append(server.get_mcp_actor())
        finally:
            server._mcp_client.reset(token)

    assert actors[0].id == f"worker:{first.client.id}"
    assert actors[1].id == f"worker:{second.client.id}"
    assert actors[0].id != actors[1].id

    client = await server.authenticate_http_bearer_token(f"Bearer {first.token}")
    token = server._mcp_client.set(client)
    try:
        names = [tool.name for tool in await server.handle_list_tools()]
        prompts = await server.handle_list_prompts()
        blocked = await server.handle_call_tool("tasks_claim", {"task_id": "task-1234"})
        blocked_infra = await server.handle_call_tool("proxmox_version", {})
    finally:
        server._mcp_client.reset(token)

    assert "tasks_get" in names
    assert "tasks_set_status" in names
    assert "tasks_claim" not in names
    assert "tasks_worker_next" in names
    assert "tasks_worker_renew" in names
    assert "tasks_worker_finish" in names
    assert "proxmox_version" in names
    assert prompts == []
    assert "worker_protocol_unavailable" in blocked[0].text
    assert "invalid_worker_lease" in blocked_infra[0].text


async def test_generic_worker_registration_is_quarantined_until_capability_grant(db_session):
    server = load_mcp_server_module()
    from app.domain.actors import Actor
    from app.services.mcp_clients import create_client_token

    issued = await create_client_token(
        db_session,
        agent_id="worker",
        client_label="Vendor-neutral adapter",
        host_fingerprint="adapter-host",
        actor=Actor(kind="telegram", id="111"),
    )
    await db_session.commit()
    client = await server.authenticate_http_bearer_token(f"Bearer {issued.token}")
    context = server._mcp_client.set(client)
    try:
        names = [tool.name for tool in await server.handle_list_tools()]
        prompts = await server.handle_list_prompts()
        blocked = await server.handle_call_tool("tasks_claim", {"task_id": "task-1234"})
    finally:
        server._mcp_client.reset(context)

    assert "tasks_get" in names
    assert "tasks_claim" not in names
    assert "tasks_worker_next" not in names
    assert "proxmox_version" not in names
    assert prompts == []
    assert "worker_protocol_unavailable" in blocked[0].text


async def test_stdio_revalidates_worker_conversion_before_discovery(monkeypatch):
    server = load_mcp_server_module()
    from app.db.models import McpClient

    client_id = "11111111-1111-4111-8111-111111111111"
    stale = McpClient(
        id=client_id,
        agent_id="codex",
        client_label="Codex worker",
        host_fingerprint="worker-host",
        token_hash="hash",
        token_hint="hint",
        capabilities=[],
        principal_id="",
    )
    converted = McpClient(
        id=client_id,
        agent_id="codex",
        client_label="Codex worker",
        host_fingerprint="worker-host",
        token_hash="hash",
        token_hint="hint",
        capabilities=["task-worker.v1"],
        principal_id=f"worker:{client_id}",
    )

    async def refreshed(_token, _agent_id=None):
        return converted

    monkeypatch.setattr(server, "authenticate_client_token", refreshed)
    client_context = server._mcp_client.set(stale)
    bearer_context = server._stdio_bearer_token.set("stdio-token")
    try:
        names = [tool.name for tool in await server.handle_list_tools()]
        actor = server.get_mcp_actor()
    finally:
        server._stdio_bearer_token.reset(bearer_context)
        server._mcp_client.reset(client_context)

    assert actor.id == f"worker:{client_id}"
    assert "tasks_get" in names
    assert "tasks_claim" not in names
    assert "tasks_worker_next" in names
    assert "proxmox_version" in names


async def test_worker_mcp_requires_lease_and_mutates_only_assigned_task(db_session):
    import json

    server = load_mcp_server_module()
    from app.domain.actors import Actor
    from app.services.mcp_clients import create_client_token, set_mcp_client_capabilities
    from app.services.remediation_workers import assign_worker_task
    from app.services.tasks_service import create_task

    operator = Actor(kind="user", id="operator")
    issued = await create_client_token(
        db_session,
        agent_id="codex",
        client_label="Codex remediation worker",
        host_fingerprint="worker-host",
        actor=operator,
    )
    await set_mcp_client_capabilities(
        db_session,
        client_id=issued.client.id,
        capabilities=["task-worker.v1"],
        actor=operator,
        confirm_worker_conversion=True,
    )
    task = await create_task(db_session, "repair", "investigate service", operator)
    task, job = await assign_worker_task(
        db_session,
        task_id=task.id,
        client_id=issued.client.id,
        actor=operator,
        expected_version=task.version,
    )
    await db_session.commit()

    client = await server.authenticate_http_bearer_token(f"Bearer {issued.token}")
    context = server._mcp_client.set(client)
    try:
        missing = await server.handle_call_tool(
            "tasks_set_status",
            {
                "task_id": task.id,
                "status": "investigating",
                "expected_version": task.version,
            },
        )
        acquired = await server.handle_call_tool("tasks_worker_next", {})
        lease = json.loads(acquired[0].text)["result"]["job"]
        wrong = await server.handle_call_tool(
            "tasks_set_status",
            {
                "task_id": task.id,
                "status": "investigating",
                "expected_version": task.version,
                "worker_job_id": job.id,
                "worker_lease_token": "wrong-token-value",
            },
        )
        changed = await server.handle_call_tool(
            "tasks_set_status",
            {
                "task_id": task.id,
                "status": "investigating",
                "expected_version": task.version,
                "worker_job_id": job.id,
                "worker_lease_token": lease["lease_token"],
            },
        )
    finally:
        server._mcp_client.reset(context)

    assert "invalid_worker_lease" in missing[0].text
    assert "invalid_worker_lease" in wrong[0].text
    changed_body = json.loads(changed[0].text)
    assert changed_body["ok"] is True
    assert changed_body["result"]["status"] == "investigating"
    assert changed_body["result"]["assigned_agent"] == f"agent:worker:{issued.client.id}"


async def test_build_streamable_http_app(monkeypatch):
    server = load_mcp_server_module()
    captured = {}

    class CapturingSessionManager:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def handle_request(self, scope, receive, send):
            raise AssertionError("request handling is outside this construction test")

        @asynccontextmanager
        async def run(self):
            yield

    monkeypatch.setattr(server, "StreamableHTTPSessionManager", CapturingSessionManager)
    monkeypatch.setenv("MCP_HTTP_PATH", "/mcp/")
    monkeypatch.setenv("MCP_HTTP_ALLOWED_HOSTS", "10.0.0.111:8765,localhost:8765")
    from app.core.settings import get_settings

    get_settings.cache_clear()
    app = server.build_streamable_http_app()
    paths = {route.path for route in app.routes}
    assert "/health" in paths
    assert "/mcp" in paths
    assert captured["stateless"] is False
    assert captured["session_idle_timeout"] == 1800


def test_streamable_http_has_bounded_graceful_shutdown(monkeypatch):
    server = load_mcp_server_module()
    import uvicorn

    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    server.run_streamable_http()

    assert captured["timeout_graceful_shutdown"] == 60
    assert captured["log_level"] == "info"


async def test_client_token_validation(db_session):
    server = load_mcp_server_module()
    from app.services.mcp_clients import consume_pairing, start_pairing

    pairing = await start_pairing(
        db_session,
        agent_id="codex",
        client_label="Codex test",
        host_fingerprint="host-a",
    )
    pairing.request.status = "approved"
    pairing.request.decided_by = "telegram:111"
    consumed = await consume_pairing(
        db_session,
        request_id=pairing.request.id,
        pairing_secret=pairing.pairing_secret,
    )
    await db_session.commit()

    assert await server.check_client_token(consumed.token, "codex") is True
    assert await server.check_client_token(consumed.token, "claude") is False


async def test_discovery_lists_only_enabled_read_tools():
    server = load_mcp_server_module()
    tools = await server.handle_list_tools()
    names = [tool.name for tool in tools]
    assert "proxmox_version" in names
    assert "pbs_summary" in names
    assert "network_clients_list" in names
    assert "cloudflare_tunnels_status" in names
    assert "cloudflare_connectors_list" in names
    assert "cloudflare_summary" in names
    assert "vps_glances_status" in names
    assert "vps_wireguard_status" in names
    assert "hosts_temperatures" in names
    assert "fritzbox_primary_temperature" in names
    assert "fritzbox_secondary_temperature" in names
    assert "proxmox_disks_temperatures" in names
    assert "mikrotik_system_health" in names
    assert "opnsense_system_temperature" in names
    assert "network_host_check" in names
    assert "notifications_status" in names
    assert "notifications_outbox_list" in names
    assert "tasks_list" in names
    assert "tasks_claim" in names
    complete_tool = next(tool for tool in tools if tool.name == "tasks_complete")
    assert "Only valid from investigating" in complete_tool.description
    for tool in tools:
        assert tool.inputSchema is not None


async def test_mcp_exposes_task_workflow_prompt():
    server = load_mcp_server_module()

    prompts = await server.handle_list_prompts()
    assert [prompt.name for prompt in prompts] == ["homelab_task_workflow"]

    prompt = await server.handle_get_prompt("homelab_task_workflow")
    text = prompt.messages[0].content.text
    assert "claimed -> investigating -> completed" in text
    assert "Never call tasks.complete directly from claimed" in text


async def test_call_routes_through_execution_core(monkeypatch):
    server = load_mcp_server_module()
    captured = {}

    from app.tools.execution import ExecutionResult
    from datetime import UTC, datetime

    async def fake_execute(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        captured.update(tool_id=tool_id, raw_input=raw_input, actor=actor, source=source, task_id=task_id)
        now = datetime.now(UTC)
        return ExecutionResult(
            ok=True, invocation_id="inv-1", tool_id=tool_id,
            started_at=now, finished_at=now, duration_ms=1, result={"answer": 42},
        )

    monkeypatch.setattr(server, "execute_tool", fake_execute)
    content = await server.handle_call_tool("proxmox_version", {"task_id": "task-1"})
    assert captured["tool_id"] == "proxmox.version"
    assert captured["raw_input"] == {}
    assert captured["actor"].kind == "agent"
    assert captured["actor"].id == "codex"
    assert captured["source"] == "mcp"
    assert captured["task_id"] == "task-1"
    assert '"ok": true' in content[0].text


async def test_unknown_mcp_tool():
    server = load_mcp_server_module()
    content = await server.handle_call_tool("rm_dash_rf", {})
    assert "unknown_tool" in content[0].text


async def test_mcp_task_handoff_tools(monkeypatch):
    server = load_mcp_server_module()
    monkeypatch.setenv("MCP_AGENT_ID", "claude")
    from app.core.settings import get_settings

    get_settings.cache_clear()

    created = await server.handle_call_tool(
        "tasks_create", {"title": "handoff", "goal": "prove task context"}
    )
    import json

    task = json.loads(created[0].text)["result"]

    claimed = await server.handle_call_tool("tasks_claim", {"task_id": task["id"]})
    claimed_task = json.loads(claimed[0].text)["result"]
    assert claimed_task["assigned_agent"] == "agent:claude"

    await server.handle_call_tool(
        "tasks_update_summary",
        {
            "task_id": task["id"],
            "summary": "Ready for Codex",
            "expected_version": claimed_task["version"],
        },
    )
    context = await server.handle_call_tool("tasks_context", {"task_id": task["id"]})
    body = json.loads(context[0].text)
    assert body["ok"] is True
    assert body["result"]["task"]["summary"] == "Ready for Codex"


def _fake_write_tool(monkeypatch, approved: bool = True):
    from app.tools import registry
    from app.tools.governance import APPROVED_WRITE_TOOLS
    from app.tools.registry import EmptyInput, ToolDefinition

    async def runner(_payload):
        return {"answer": 42}

    tool = ToolDefinition(
        id="test.write",
        name="Test Write",
        description="test write tool",
        provider_id="test",
        category="test",
        mode="write",
        risk="low",
        enabled=True,
        timeout_seconds=5.0,
        input_model=EmptyInput,
        runner=runner,
    )
    monkeypatch.setattr(registry, "_TOOLS", [*registry._TOOLS, tool])
    if approved:
        monkeypatch.setitem(APPROVED_WRITE_TOOLS, tool.id, "docs/decisions/0004.md")
    return tool


async def test_worker_approval_request_is_lease_bound(db_session, monkeypatch):
    import json

    server = load_mcp_server_module()
    tool = _fake_write_tool(monkeypatch, approved=True)
    from app.domain.actors import Actor
    from app.services.mcp_clients import create_client_token, set_mcp_client_capabilities
    from app.services.remediation_workers import assign_worker_task, worker_next
    from app.services.tasks_service import create_task

    async def no_notify(_approval, _safe_input):
        return False

    monkeypatch.setattr("app.services.approvals_service._notify_operator", no_notify)
    operator = Actor(kind="user", id="operator")
    issued = await create_client_token(
        db_session,
        agent_id="codex",
        client_label="approval worker",
        host_fingerprint="approval-host",
        actor=operator,
    )
    await set_mcp_client_capabilities(
        db_session,
        client_id=issued.client.id,
        capabilities=["task-worker.v1"],
        actor=operator,
        confirm_worker_conversion=True,
    )
    task = await create_task(db_session, "approved repair", "request write", operator)
    task, job = await assign_worker_task(
        db_session,
        task_id=task.id,
        client_id=issued.client.id,
        actor=operator,
    )
    acquired = await worker_next(db_session, client_id=issued.client.id)
    lease = acquired["job"]
    await db_session.commit()

    client = await server.authenticate_http_bearer_token(f"Bearer {issued.token}")
    context = server._mcp_client.set(client)
    try:
        missing = await server.handle_call_tool(
            "approvals_request",
            {"tool_id": tool.id, "input": {}, "task_id": task.id},
        )
        valid = await server.handle_call_tool(
            "approvals_request",
            {
                "tool_id": tool.id,
                "input": {},
                "task_id": task.id,
                "worker_job_id": job.id,
                "worker_lease_token": lease["lease_token"],
            },
        )
    finally:
        server._mcp_client.reset(context)

    assert "invalid_worker_lease" in missing[0].text
    body = json.loads(valid[0].text)
    assert body["ok"] is True
    assert body["result"]["task_id"] == task.id
    from app.db.models import Approval

    approval = await db_session.get(Approval, body["result"]["id"])
    assert approval.worker_job_id == job.id
    assert approval.worker_lease_generation == job.lease_generation


async def test_discovery_exposes_approval_tools_and_approved_writes(monkeypatch):
    server = load_mcp_server_module()
    _fake_write_tool(monkeypatch, approved=True)

    tools = await server.handle_list_tools()
    by_name = {tool.name: tool for tool in tools}
    assert "approvals_request" in by_name
    assert "approvals_get" in by_name
    assert "test_write" in by_name
    schema = by_name["test_write"].inputSchema
    assert "approval_id" in schema["properties"]
    assert "task_id" in schema["properties"]


async def test_discovery_hides_unapproved_write_tools(monkeypatch):
    server = load_mcp_server_module()
    _fake_write_tool(monkeypatch, approved=False)

    tools = await server.handle_list_tools()
    assert "test_write" not in [tool.name for tool in tools]


async def test_write_call_without_approval_rejected(monkeypatch, db_session):
    server = load_mcp_server_module()
    _fake_write_tool(monkeypatch)

    import json

    content = await server.handle_call_tool("test_write", {})
    body = json.loads(content[0].text)
    assert body["ok"] is False
    assert body["error"]["code"] == "approval_required"


async def test_mcp_approval_request_approve_execute_cycle(monkeypatch, db_session):
    server = load_mcp_server_module()
    _fake_write_tool(monkeypatch)

    import json

    from app.domain.actors import Actor
    from app.services.approvals_service import decide_approval

    requested = await server.handle_call_tool(
        "approvals_request", {"tool_id": "test_write", "input": {}}
    )
    body = json.loads(requested[0].text)
    assert body["ok"] is True
    approval_id = body["result"]["id"]
    assert body["result"]["status"] == "pending"

    polled = await server.handle_call_tool("approvals_get", {"approval_id": approval_id})
    assert json.loads(polled[0].text)["result"]["status"] == "pending"

    operator = Actor(kind="telegram", id="42", label="telegram operator")
    await decide_approval(db_session, approval_id=approval_id, approve=True, actor=operator)
    await db_session.commit()

    executed = await server.handle_call_tool("test_write", {"approval_id": approval_id})
    executed_body = json.loads(executed[0].text)
    assert executed_body["ok"] is True
    assert executed_body["result"] == {"answer": 42}

    replay = await server.handle_call_tool("test_write", {"approval_id": approval_id})
    assert json.loads(replay[0].text)["error"]["code"] == "approval_required"

    consumed = await server.handle_call_tool("approvals_get", {"approval_id": approval_id})
    assert json.loads(consumed[0].text)["result"]["status"] == "consumed"


async def test_mcp_approval_request_rejects_read_tool(monkeypatch, db_session):
    server = load_mcp_server_module()

    import json

    content = await server.handle_call_tool(
        "approvals_request", {"tool_id": "proxmox.version", "input": {}}
    )
    body = json.loads(content[0].text)
    assert body["ok"] is False
    assert body["error"]["code"] == "not_approvable"
