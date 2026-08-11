"""Homelab Console MCP server (stdio or streamable HTTP).

An authenticated adapter over the shared execution core: it discovers tools
from the registry and routes every call through execute_tool. It contains no
provider logic and cannot bypass enablement, risk policy, validation,
redaction or audit.
"""

from __future__ import annotations

import asyncio
import argparse
from contextvars import ContextVar
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
import sys
import socket
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]
API_APP = ROOT / "apps" / "api"
sys.path.insert(0, str(API_APP))

import mcp.types as types  # noqa: E402
from mcp.server.lowlevel import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402

from app.core.settings import get_settings  # noqa: E402
from app.db.session import init_db  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.domain.actors import Actor  # noqa: E402
from app.services.mcp_clients import (  # noqa: E402
    McpClientError,
    TASK_WORKER_CAPABILITY,
    VALID_MCP_AGENT_IDS,
    consume_pairing,
    mcp_client_actor,
    mcp_client_has_capability,
    start_pairing,
    validate_client_token_any_agent,
    validate_client_token,
)
from app.services.remediation_workers import (  # noqa: E402
    RemediationWorkerError,
    validate_worker_lease,
    worker_finish,
    worker_next,
    worker_renew,
)
from app.services.approvals_service import (  # noqa: E402
    ApprovalError,
    approval_public,
    get_approval,
    request_approval,
)
from app.services.task_context import compile_task_context  # noqa: E402
from app.services.tasks_service import (  # noqa: E402
    TaskServiceError,
    add_check,
    add_finding,
    add_note,
    check_public,
    claim_task,
    complete_check,
    complete_task,
    create_task,
    finding_public,
    get_task,
    list_checks,
    list_events,
    list_findings,
    list_tasks,
    release_task,
    reopen_task,
    resolve_finding,
    set_status,
    skip_check,
    task_public,
    update_summary,
)
from app.tools.execution import execute_tool  # noqa: E402
from app.tools.registry import list_tools  # noqa: E402

MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS = 1800
MCP_HTTP_GRACEFUL_SHUTDOWN_SECONDS = 60

server = Server("homelab-console")
_http_actor: ContextVar[Actor | None] = ContextVar("homelab_mcp_http_actor", default=None)
_mcp_client = ContextVar("homelab_mcp_client", default=None)
_stdio_bearer_token: ContextVar[str] = ContextVar("homelab_mcp_stdio_bearer_token", default="")


class _McpInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskListInput(_McpInput):
    status: str | None = None
    assigned_agent: str | None = Field(default=None, max_length=80)
    limit: int = Field(default=100, ge=1, le=100)


class TaskIdInput(_McpInput):
    task_id: str = Field(min_length=8, max_length=64)
    worker_job_id: str | None = Field(default=None, min_length=8, max_length=64)
    worker_lease_token: str | None = Field(default=None, min_length=16, max_length=256)


class TaskCreateInput(_McpInput):
    title: str = Field(min_length=1, max_length=256)
    goal: str = Field(default="", max_length=4000)


class VersionedTaskInput(TaskIdInput):
    expected_version: int | None = Field(default=None, ge=1)


class TaskReleaseInput(VersionedTaskInput):
    handoff_summary: str = Field(min_length=1, max_length=4000)


class TaskStatusInput(VersionedTaskInput):
    status: str = Field(min_length=1, max_length=32)


class TaskSummaryInput(VersionedTaskInput):
    summary: str = Field(default="", max_length=8000)


class TaskNoteInput(TaskIdInput):
    note: str = Field(min_length=1, max_length=4000)


class TaskFindingInput(TaskIdInput):
    severity: str = Field(min_length=1, max_length=16)
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=4000)
    tool_invocation_id: str | None = Field(default=None, max_length=64)


class TaskResolveFindingInput(TaskIdInput):
    finding_id: str = Field(min_length=8, max_length=64)


class TaskCheckInput(TaskIdInput):
    description: str = Field(min_length=1, max_length=512)


class TaskCheckIdInput(TaskIdInput):
    check_id: str = Field(min_length=8, max_length=64)


class TaskSkipCheckInput(TaskCheckIdInput):
    reason: str = Field(min_length=1, max_length=1000)


class ApprovalRequestInput(_McpInput):
    tool_id: str = Field(min_length=1, max_length=128)
    input: dict = Field(default_factory=dict)
    task_id: str | None = None
    worker_job_id: str | None = Field(default=None, min_length=8, max_length=64)
    worker_lease_token: str | None = Field(default=None, min_length=16, max_length=256)


class ApprovalGetInput(_McpInput):
    approval_id: str = Field(min_length=8, max_length=64)


class WorkerNextInput(_McpInput):
    pass


class WorkerRenewInput(_McpInput):
    job_id: str = Field(min_length=8, max_length=64)
    lease_token: str = Field(min_length=16, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=36)


class WorkerFinishInput(WorkerRenewInput):
    outcome: str = Field(min_length=1, max_length=16)
    expected_task_version: int = Field(ge=1)
    error_code: str = Field(default="", max_length=64)


APPROVAL_TOOL_MODELS: dict[str, tuple[str, type[BaseModel]]] = {
    "approvals.request": (
        "Request operator approval for one write/high-risk tool invocation with the exact input. "
        "The operator decides on Telegram or the console; poll approvals.get, then call the tool "
        "with approval_id. The approval is single-use and input-bound.",
        ApprovalRequestInput,
    ),
    "approvals.get": (
        "Read one approval request's current status (pending/approved/denied/expired/consumed).",
        ApprovalGetInput,
    ),
}


TASK_TOOL_MODELS: dict[str, tuple[str, type[BaseModel]]] = {
    "tasks.list": ("List persistent tasks with limited filters.", TaskListInput),
    "tasks.get": ("Read one persistent task.", TaskIdInput),
    "tasks.context": ("Read compact handoff context for a task.", TaskIdInput),
    "tasks.events.list": ("List append-only task events.", TaskIdInput),
    "tasks.findings.list": ("List task findings.", TaskIdInput),
    "tasks.checks.list": ("List task checks.", TaskIdInput),
    "tasks.create": ("Create a persistent task.", TaskCreateInput),
    "tasks.claim": (
        "Claim an open task for the configured MCP agent. After claiming, set status to investigating before doing work.",
        TaskIdInput,
    ),
    "tasks.release": ("Release a claimed task with a handoff summary for the next agent/operator.", TaskReleaseInput),
    "tasks.set_status": (
        "Set task status through validated transitions. To finish a claimed task, first set status=investigating, then call tasks.complete.",
        TaskStatusInput,
    ),
    "tasks.update_summary": ("Update task summary.", TaskSummaryInput),
    "tasks.add_note": ("Append a task note event.", TaskNoteInput),
    "tasks.add_finding": ("Add a structured task finding.", TaskFindingInput),
    "tasks.resolve_finding": ("Mark a task finding resolved.", TaskResolveFindingInput),
    "tasks.add_check": ("Add a task checklist item.", TaskCheckInput),
    "tasks.complete_check": ("Complete a task checklist item.", TaskCheckIdInput),
    "tasks.skip_check": ("Skip a task checklist item with a reason.", TaskSkipCheckInput),
    "tasks.complete": (
        "Complete a task. Only valid from investigating; claimed -> completed is invalid. If task is claimed, call tasks.set_status(status='investigating') first.",
        VersionedTaskInput,
    ),
    "tasks.reopen": ("Reopen a completed task.", VersionedTaskInput),
}

WORKER_READ_TASK_TOOLS = frozenset(
    {
        "tasks.list",
        "tasks.get",
        "tasks.context",
        "tasks.events.list",
        "tasks.findings.list",
        "tasks.checks.list",
    }
)
WORKER_BLOCKED_TASK_TOOLS = frozenset({"tasks.create", "tasks.claim", "tasks.reopen"})
WORKER_TOOL_MODELS: dict[str, tuple[str, type[BaseModel]]] = {
    "tasks.worker.next": (
        "Acquire the next operator-assigned remediation job and its bounded lease.",
        WorkerNextInput,
    ),
    "tasks.worker.renew": (
        "Renew an active remediation lease using an idempotency key.",
        WorkerRenewInput,
    ),
    "tasks.worker.finish": (
        "Finish or retry a remediation job after canonical task state is updated.",
        WorkerFinishInput,
    ),
}


MCP_TASK_WORKFLOW_PROMPT = """You are connected to Homelab Console through MCP.

Task state is canonical in the backend. Follow this workflow exactly:

1. Start with tasks.list or tasks.get/tasks.context. Do not assume task state.
2. Claim open work with tasks.claim before mutating it.
3. After claiming, move the task to investigating with tasks.set_status(status="investigating").
4. Run infrastructure read-only tools with task_id so invocations attach to the task.
5. Record findings/checks/notes/summary as you work.
6. To complete work:
   - complete or skip pending checks where appropriate;
   - update the summary with what changed and current evidence;
   - ensure the task status is investigating;
   - call tasks.complete with the latest expected_version.
7. Never call tasks.complete directly from claimed. The valid path is claimed -> investigating -> completed.
8. If handing off, update summary and tasks.release with a concise handoff_summary instead of completing.
9. If a call returns version_conflict or a tool response includes task_version, use the newest version before the next task mutation.
10. If a transition is rejected, read tasks.context and choose a valid next transition; do not pretend the task is complete.

Important valid transitions:
- open -> claimed
- claimed -> investigating or open/cancelled
- investigating -> waiting_operator, blocked, completed, or cancelled
- waiting_operator/blocked -> investigating
- completed -> open via tasks.reopen

Write tools (ADR 0004): every write or high-risk tool needs a single-use,
input-bound operator approval. Call approvals.request with the exact tool_id
and input first; the human operator decides on Telegram or the console. Poll
approvals.get until status is approved (requests expire in minutes), then
call the tool once with that approval_id and the same exact input. Never
retry a consumed approval; request a new one. A denied or expired request is
an operator decision — respect it and record it on the task instead of
retrying.
"""


def _exposed_tools():
    """Enabled tools are visible to MCP clients. Registry policy already
    forces unapproved write tools disabled, so an enabled write tool is by
    definition operator-approved (ADR 0004); it still requires a consumed
    per-invocation approval_id at execution time."""
    return [tool for tool in list_tools() if tool.enabled]


def _needs_approval(tool) -> bool:
    return tool.mode == "write" or tool.risk == "high"


def _mcp_name(tool_id: str) -> str:
    return tool_id.replace(".", "_")


def _tool_id_by_name(name: str) -> str | None:
    for tool in _exposed_tools():
        if _mcp_name(tool.id) == name:
            return tool.id
    return None


def _authorization_bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return ""
    return token


async def authenticate_http_bearer_token(authorization: str | None):
    token = _authorization_bearer(authorization)
    if not token:
        return None
    async with get_session_factory()() as db:
        client = await validate_client_token_any_agent(db, token=token)
        if client is None:
            await db.rollback()
            return None
        await db.commit()
        return client


async def check_http_bearer_token(authorization: str | None) -> bool:
    return await authenticate_http_bearer_token(authorization) is not None


def _default_client_token_path(agent_id: str) -> Path:
    configured = get_settings().mcp_client_token_path.strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "homelab-console" / f"mcp-{agent_id}.token"


def _read_client_token(agent_id: str) -> str:
    env_token = os.environ.get("MCP_CLIENT_TOKEN", "").strip()
    if env_token:
        return env_token
    path = _default_client_token_path(agent_id)
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _write_client_token(agent_id: str, token: str) -> Path:
    path = _default_client_token_path(agent_id)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _client_label(agent_id: str) -> str:
    configured = get_settings().mcp_client_label.strip()
    if configured:
        return configured
    return f"{agent_id}@{socket.gethostname()}"


def _host_fingerprint() -> str:
    return socket.gethostname()


async def authenticate_client_token(provided: str | None, agent_id: str | None = None):
    agent_id = agent_id or get_mcp_agent_id()
    if not provided:
        return None
    async with get_session_factory()() as db:
        client = await validate_client_token(db, token=provided, agent_id=agent_id)
        if client is None:
            await db.rollback()
            return None
        await db.commit()
        return client


async def check_client_token(provided: str | None, agent_id: str | None = None) -> bool:
    return await authenticate_client_token(provided, agent_id) is not None


async def ensure_mcp_registration():
    agent_id = get_mcp_agent_id()
    client_token = _read_client_token(agent_id)
    client = await authenticate_client_token(client_token, agent_id)
    if client is not None:
        return client

    async with get_session_factory()() as db:
        pairing = await start_pairing(
            db,
            agent_id=agent_id,
            client_label=_client_label(agent_id),
            host_fingerprint=_host_fingerprint(),
        )
        await db.commit()

    print(
        f"MCP pairing requested for {agent_id}; approve it in Telegram. "
        f"Request id: {pairing.request.id[:8]}",
        file=sys.stderr,
    )
    if pairing.request.delivery_status != "sent":
        print("MCP pairing could not be delivered via Telegram.", file=sys.stderr)
        raise SystemExit(2)

    timeout = max(30, int(get_settings().mcp_pairing_timeout_seconds))
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(2)
        async with get_session_factory()() as db:
            try:
                result = await consume_pairing(
                    db,
                    request_id=pairing.request.id,
                    pairing_secret=pairing.pairing_secret,
                )
            except McpClientError as exc:
                await db.rollback()
                if exc.code == "pairing_not_approved":
                    continue
                print(f"MCP pairing failed: {exc.message}", file=sys.stderr)
                raise SystemExit(2) from exc
            await db.commit()
            path = _write_client_token(agent_id, result.token)
            print(f"MCP client registered; token saved to {path}", file=sys.stderr)
            return result.client

    print("MCP pairing timed out.", file=sys.stderr)
    raise SystemExit(2)


def get_mcp_agent_id() -> str:
    agent_id = get_settings().mcp_agent_id.strip().lower()
    if agent_id not in VALID_MCP_AGENT_IDS:
        raise RuntimeError(
            "MCP_AGENT_ID must be one of: claude, fixer, codex, cline, opencode, worker"
        )
    return agent_id


def get_mcp_actor() -> Actor:
    client = _mcp_client.get()
    if client is not None:
        return mcp_client_actor(client)
    actor = _http_actor.get()
    if actor is not None:
        return actor
    agent_id = get_mcp_agent_id()
    return Actor(kind="agent", id=agent_id, label=f"MCP {agent_id}")


def _is_worker_registration() -> bool:
    client = _mcp_client.get()
    return client is not None and (
        client.agent_id == "worker" or client.principal_id.startswith("worker:")
    )


def _worker_capability_active() -> bool:
    client = _mcp_client.get()
    return (
        client is not None
        and _is_worker_registration()
        and client.revoked_at is None
        and mcp_client_has_capability(client, TASK_WORKER_CAPABILITY)
    )


def _worker_client_id() -> str:
    client = _mcp_client.get()
    if client is None or not _worker_capability_active():
        raise RemediationWorkerError(
            "worker_capability_required", "worker capability is required"
        )
    return client.id


async def _refresh_stdio_authentication() -> bool:
    """Revalidate persistent stdio sessions before every MCP operation.

    HTTP authenticates each request in its ASGI wrapper. Stdio otherwise keeps
    one process alive indefinitely, so a capability conversion, token rotation
    or revocation must be observed without trusting the startup snapshot.
    """
    token = _stdio_bearer_token.get()
    if not token:
        return True
    client = await authenticate_client_token(token, get_mcp_agent_id())
    _mcp_client.set(client)
    return client is not None


def _infra_input_schema(tool) -> dict:
    schema = deepcopy(tool.input_model.model_json_schema())
    properties = dict(schema.get("properties") or {})
    properties["task_id"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
        "description": "Persistent task context metadata. It is not passed to the provider.",
    }
    properties["worker_job_id"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
        "description": "Required remediation job metadata for worker task-bound execution.",
    }
    properties["worker_lease_token"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
        "description": "Opaque active remediation lease; never passed to the provider.",
    }
    if _needs_approval(tool):
        properties["approval_id"] = {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "description": (
                "Consumed operator approval for this exact invocation. "
                "Obtain it with approvals.request and poll approvals.get until approved."
            ),
        }
    schema["properties"] = properties
    return schema


def _task_tool_id_by_name(name: str) -> str | None:
    for tool_id in TASK_TOOL_MODELS:
        if _mcp_name(tool_id) == name:
            return tool_id
    return None


def _approval_tool_id_by_name(name: str) -> str | None:
    for tool_id in APPROVAL_TOOL_MODELS:
        if _mcp_name(tool_id) == name:
            return tool_id
    return None


def _worker_tool_id_by_name(name: str) -> str | None:
    for tool_id in WORKER_TOOL_MODELS:
        if _mcp_name(tool_id) == name:
            return tool_id
    return None


def _registry_tool_id(raw: str) -> str:
    """Accept both registry ids (dots) and MCP names (underscores)."""
    if "." in raw:
        return raw
    return _tool_id_by_name(raw) or raw


async def _handle_approval_tool(tool_id: str, arguments: dict) -> list[types.TextContent]:
    _description, model = APPROVAL_TOOL_MODELS[tool_id]
    try:
        payload = model.model_validate(arguments)
    except Exception as exc:
        return _text(_error_payload("invalid_input", exc.__class__.__name__))

    actor = get_mcp_actor()
    async with get_session_factory()() as db:
        if tool_id == "approvals.request":
            try:
                if _is_worker_registration():
                    if not payload.task_id or not payload.worker_job_id or not payload.worker_lease_token:
                        raise RemediationWorkerError(
                            "invalid_worker_lease",
                            "worker approval requests require task and lease metadata",
                        )
                    worker_job = await validate_worker_lease(
                        db,
                        task_id=payload.task_id,
                        worker_job_id=payload.worker_job_id,
                        worker_lease_token=payload.worker_lease_token,
                        client_id=_worker_client_id(),
                    )
                approval = await request_approval(
                    db,
                    tool_id=_registry_tool_id(payload.tool_id),
                    raw_input=payload.input,
                    actor=actor,
                    task_id=payload.task_id,
                    source="mcp",
                    worker_job=worker_job if _is_worker_registration() else None,
                )
            except (ApprovalError, RemediationWorkerError) as exc:
                await db.rollback()
                return _text(_error_payload(exc.code, exc.message))
            result = approval_public(approval)
            await db.commit()
        else:
            approval = await get_approval(db, payload.approval_id)
            if approval is None:
                return _text(_error_payload("unknown_approval", "unknown approval id"))
            result = approval_public(approval)
    return _text({"ok": True, "result": result})


async def _handle_worker_tool(tool_id: str, arguments: dict) -> list[types.TextContent]:
    _description, model = WORKER_TOOL_MODELS[tool_id]
    try:
        payload = model.model_validate(arguments)
        client_id = _worker_client_id()
        async with get_session_factory()() as db:
            if tool_id == "tasks.worker.next":
                result = await worker_next(db, client_id=client_id)
            elif tool_id == "tasks.worker.renew":
                result = await worker_renew(
                    db,
                    client_id=client_id,
                    job_id=payload.job_id,
                    lease_token=payload.lease_token,
                    idempotency_key=payload.idempotency_key,
                )
            else:
                result = await worker_finish(
                    db,
                    client_id=client_id,
                    job_id=payload.job_id,
                    lease_token=payload.lease_token,
                    idempotency_key=payload.idempotency_key,
                    outcome=payload.outcome,
                    expected_task_version=payload.expected_task_version,
                    error_code=payload.error_code,
                )
            await db.commit()
        return _text({"ok": True, "result": result})
    except RemediationWorkerError as exc:
        return _text(_error_payload(exc.code, exc.message))
    except Exception as exc:
        if exc.__class__.__module__.startswith("pydantic"):
            return _text(_error_payload("invalid_input", exc.__class__.__name__))
        raise


def _error_payload(code: str, message: str, invocation_id: str | None = None) -> dict:
    payload = {"ok": False, "error": {"code": code, "message": message}}
    if invocation_id:
        payload["invocation_id"] = invocation_id
    return payload


def _text(payload: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, default=str))]


@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    if not await _refresh_stdio_authentication():
        return []
    if _is_worker_registration():
        return []
    return [
        types.Prompt(
            name="homelab_task_workflow",
            title="Homelab Task Workflow",
            description=(
                "Initial operating instructions for Homelab MCP agents: "
                "claim, move to investigating, use task_id, update evidence, complete only from investigating."
            ),
        )
    ]


@server.get_prompt()
async def handle_get_prompt(name: str, arguments: dict | None = None) -> types.GetPromptResult:
    if not await _refresh_stdio_authentication():
        raise ValueError("unauthorized MCP client")
    if _is_worker_registration() or name != "homelab_task_workflow":
        raise ValueError(f"unknown prompt: {name}")
    return types.GetPromptResult(
        description="Homelab Console MCP task lifecycle instructions.",
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=MCP_TASK_WORKFLOW_PROMPT),
            )
        ],
    )


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    if not await _refresh_stdio_authentication():
        return []
    worker_registration = _is_worker_registration()
    worker_active = _worker_capability_active()
    infra_tools = [] if worker_registration and not worker_active else [
        types.Tool(
            name=_mcp_name(tool.id),
            description=tool.description,
            inputSchema=_infra_input_schema(tool),
        )
        for tool in _exposed_tools()
    ]
    task_tools = [
        types.Tool(
            name=_mcp_name(tool_id),
            description=description,
            inputSchema=model.model_json_schema(),
        )
        for tool_id, (description, model) in TASK_TOOL_MODELS.items()
        if (
            not worker_registration
            or tool_id in WORKER_READ_TASK_TOOLS
            or (worker_active and tool_id not in WORKER_BLOCKED_TASK_TOOLS)
        )
    ]
    approval_tools = [] if worker_registration and not worker_active else [
        types.Tool(
            name=_mcp_name(tool_id),
            description=description,
            inputSchema=model.model_json_schema(),
        )
        for tool_id, (description, model) in APPROVAL_TOOL_MODELS.items()
    ]
    worker_tools = [
        types.Tool(
            name=_mcp_name(tool_id),
            description=description,
            inputSchema=model.model_json_schema(),
        )
        for tool_id, (description, model) in WORKER_TOOL_MODELS.items()
    ] if worker_active else []
    return infra_tools + task_tools + approval_tools + worker_tools


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    if not await _refresh_stdio_authentication():
        return _text(_error_payload("unauthorized", "MCP client token is invalid or revoked"))
    approval_tool_id = _approval_tool_id_by_name(name)
    task_tool_id = _task_tool_id_by_name(name)
    infra_tool_id = _tool_id_by_name(name)
    worker_tool_id = _worker_tool_id_by_name(name)
    if worker_tool_id is not None:
        if not _worker_capability_active():
            return _text(_error_payload("worker_capability_required", "worker capability is required"))
        return await _handle_worker_tool(worker_tool_id, arguments or {})

    if _is_worker_registration():
        if task_tool_id in WORKER_BLOCKED_TASK_TOOLS:
            return _text(
                _error_payload(
                    "worker_protocol_unavailable",
                    "remediation workers cannot claim, create or reopen arbitrary tasks",
                )
            )
        if not _worker_capability_active() and (
            approval_tool_id is not None
            or infra_tool_id is not None
            or (task_tool_id is not None and task_tool_id not in WORKER_READ_TASK_TOOLS)
        ):
            return _text(_error_payload("worker_capability_required", "worker capability is required"))
    if approval_tool_id is not None:
        return await _handle_approval_tool(approval_tool_id, arguments or {})

    if task_tool_id is not None:
        return await _handle_task_tool(task_tool_id, arguments or {})

    tool_id = infra_tool_id
    if tool_id is None:
        return _text(_error_payload("unknown_tool", f"unknown tool: {name}"))

    raw_arguments = dict(arguments or {})
    task_id = raw_arguments.pop("task_id", None)
    approval_id = raw_arguments.pop("approval_id", None)
    worker_job_id = raw_arguments.pop("worker_job_id", None)
    worker_lease_token = raw_arguments.pop("worker_lease_token", None)
    worker_client_id = None
    if _is_worker_registration():
        if not task_id or not worker_job_id or not worker_lease_token:
            return _text(
                _error_payload(
                    "invalid_worker_lease",
                    "worker infrastructure calls require task and lease metadata",
                )
            )
        worker_client_id = _worker_client_id()
    worker_execution = {}
    if worker_client_id is not None:
        worker_execution = {
            "worker_client_id": worker_client_id,
            "worker_job_id": worker_job_id,
            "worker_lease_token": worker_lease_token,
        }
    result = await execute_tool(
        tool_id,
        raw_arguments,
        get_mcp_actor(),
        source="mcp",
        task_id=task_id,
        approval_id=approval_id,
        **worker_execution,
    )
    if result.ok:
        payload = {"ok": True, "invocation_id": result.invocation_id, "result": result.result}
    else:
        payload = _error_payload(
            result.error.code,
            result.error.message,
            invocation_id=result.invocation_id,
        )
    if result.task_version is not None:
        payload["task_version"] = result.task_version
    return _text(payload)


async def _handle_task_tool(tool_id: str, arguments: dict) -> list[types.TextContent]:
    _description, model = TASK_TOOL_MODELS[tool_id]
    try:
        payload = model.model_validate(arguments)
    except Exception as exc:
        return _text(_error_payload("invalid_input", exc.__class__.__name__))

    actor = get_mcp_actor()
    agent = f"agent:{actor.id}"
    try:
        async with get_session_factory()() as db:
            if _is_worker_registration() and tool_id not in WORKER_READ_TASK_TOOLS:
                worker_job_id = getattr(payload, "worker_job_id", None)
                worker_lease_token = getattr(payload, "worker_lease_token", None)
                task_id = getattr(payload, "task_id", None)
                if not task_id or not worker_job_id or not worker_lease_token:
                    raise RemediationWorkerError(
                        "invalid_worker_lease",
                        "worker task mutations require active lease metadata",
                    )
                await validate_worker_lease(
                    db,
                    task_id=task_id,
                    worker_job_id=worker_job_id,
                    worker_lease_token=worker_lease_token,
                    client_id=_worker_client_id(),
                )
            if tool_id == "tasks.list":
                rows = await list_tasks(
                    db,
                    status=payload.status,
                    assigned_agent=payload.assigned_agent,
                    limit=payload.limit,
                )
                result = [task_public(task) for task in rows]
            elif tool_id == "tasks.get":
                task = await get_task(db, payload.task_id)
                if task is None:
                    raise TaskServiceError("unknown_task", "unknown task")
                result = task_public(task)
            elif tool_id == "tasks.context":
                result = await compile_task_context(db, payload.task_id)
            elif tool_id == "tasks.events.list":
                result = [
                    {
                        "id": event.id,
                        "kind": event.kind,
                        "payload": event.payload,
                        "created_at": event.created_at,
                    }
                    for event in await list_events(db, payload.task_id)
                ]
            elif tool_id == "tasks.findings.list":
                result = [finding_public(finding) for finding in await list_findings(db, payload.task_id)]
            elif tool_id == "tasks.checks.list":
                result = [check_public(check) for check in await list_checks(db, payload.task_id)]
            elif tool_id == "tasks.create":
                task = await create_task(db, payload.title, payload.goal, actor, source="mcp")
                result = task_public(task)
            elif tool_id == "tasks.claim":
                task = await claim_task(db, payload.task_id, agent, actor, source="mcp")
                result = task_public(task)
            elif tool_id == "tasks.release":
                task = await release_task(
                    db,
                    payload.task_id,
                    actor,
                    expected_version=payload.expected_version,
                    handoff_summary=payload.handoff_summary,
                    source="mcp",
                )
                result = task_public(task)
            elif tool_id == "tasks.set_status":
                task = await set_status(
                    db,
                    payload.task_id,
                    payload.status,
                    actor,
                    expected_version=payload.expected_version,
                    source="mcp",
                )
                result = task_public(task)
            elif tool_id == "tasks.update_summary":
                task = await update_summary(
                    db,
                    payload.task_id,
                    payload.summary,
                    actor,
                    expected_version=payload.expected_version,
                    source="mcp",
                )
                result = task_public(task)
            elif tool_id == "tasks.add_note":
                event = await add_note(db, payload.task_id, payload.note, actor, source="mcp")
                result = {"id": event.id, "kind": event.kind, "payload": event.payload}
            elif tool_id == "tasks.add_finding":
                finding = await add_finding(
                    db,
                    payload.task_id,
                    payload.severity,
                    payload.title,
                    payload.description,
                    actor,
                    source="mcp",
                    tool_invocation_id=payload.tool_invocation_id,
                )
                result = finding_public(finding)
            elif tool_id == "tasks.resolve_finding":
                finding = await resolve_finding(
                    db, payload.task_id, payload.finding_id, actor, source="mcp"
                )
                result = finding_public(finding)
            elif tool_id == "tasks.add_check":
                check = await add_check(db, payload.task_id, payload.description, actor, source="mcp")
                result = check_public(check)
            elif tool_id == "tasks.complete_check":
                check = await complete_check(db, payload.task_id, payload.check_id, actor, source="mcp")
                result = check_public(check)
            elif tool_id == "tasks.skip_check":
                check = await skip_check(
                    db,
                    payload.task_id,
                    payload.check_id,
                    actor,
                    payload.reason,
                    source="mcp",
                )
                result = check_public(check)
            elif tool_id == "tasks.complete":
                task = await complete_task(
                    db,
                    payload.task_id,
                    actor,
                    expected_version=payload.expected_version,
                    source="mcp",
                )
                result = task_public(task)
            elif tool_id == "tasks.reopen":
                task = await reopen_task(
                    db,
                    payload.task_id,
                    actor,
                    expected_version=payload.expected_version,
                    source="mcp",
                )
                result = task_public(task)
            else:
                return _text(_error_payload("unknown_tool", f"unknown tool: {tool_id}"))
            await db.commit()
            return _text({"ok": True, "result": result})
    except (TaskServiceError, RemediationWorkerError) as exc:
        return _text(_error_payload(exc.code, exc.message))


def build_streamable_http_app() -> Starlette:
    settings = get_settings()
    path = settings.mcp_http_path if settings.mcp_http_path.startswith("/") else f"/{settings.mcp_http_path}"
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.mcp_http_allowed_host_list,
        allowed_origins=settings.mcp_http_allowed_origin_list,
    )
    session_manager = StreamableHTTPSessionManager(
        app=server,
        stateless=False,
        security_settings=security,
        session_idle_timeout=MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS,
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        await init_db()
        async with session_manager.run():
            yield

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "transport": "streamable_http", "path": path})

    async def handle_mcp(scope, receive, send) -> None:
        request = Request(scope, receive)
        client = await authenticate_http_bearer_token(request.headers.get("authorization"))
        if client is None:
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        token = _http_actor.set(
            mcp_client_actor(client)
        )
        client_token = _mcp_client.set(client)
        try:
            await session_manager.handle_request(scope, receive, send)
        finally:
            _mcp_client.reset(client_token)
            _http_actor.reset(token)

    class AlreadyHandledResponse(Response):
        async def __call__(self, scope, receive, send) -> None:
            return

    async def handle_mcp_no_slash(request: Request) -> Response:
        await handle_mcp(request.scope, request.receive, request._send)
        return AlreadyHandledResponse()

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route(path.rstrip("/"), handle_mcp_no_slash, methods=["GET", "POST", "DELETE"]),
            Mount(path, app=handle_mcp),
        ],
        lifespan=lifespan,
    )
    app.router.redirect_slashes = False
    return app


async def run_stdio() -> None:
    try:
        get_mcp_agent_id()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    await init_db()
    client = await ensure_mcp_registration()
    client_context_token = _mcp_client.set(client)
    bearer_context_token = _stdio_bearer_token.set(_read_client_token(get_mcp_agent_id()))
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        _stdio_bearer_token.reset(bearer_context_token)
        _mcp_client.reset(client_context_token)


def run_streamable_http() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        build_streamable_http_app(),
        host=settings.mcp_http_host,
        port=settings.mcp_http_port,
        log_level="info",
        # Persistent Streamable HTTP GET/SSE connections are expected to stay
        # open. Bound Uvicorn's connection drain so they cannot block systemd
        # shutdown until its harder TimeoutStopSec=90 deadline.
        timeout_graceful_shutdown=MCP_HTTP_GRACEFUL_SHUTDOWN_SECONDS,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Homelab Console MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
    )
    args = parser.parse_args()
    if args.transport == "streamable-http":
        run_streamable_http()
        return
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
