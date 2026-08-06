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


async def test_build_streamable_http_app(monkeypatch):
    server = load_mcp_server_module()
    monkeypatch.setenv("MCP_HTTP_PATH", "/mcp/")
    monkeypatch.setenv("MCP_HTTP_ALLOWED_HOSTS", "10.0.0.111:8765,localhost:8765")
    from app.core.settings import get_settings

    get_settings.cache_clear()
    app = server.build_streamable_http_app()
    paths = {route.path for route in app.routes}
    assert "/health" in paths
    assert "/mcp" in paths


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
