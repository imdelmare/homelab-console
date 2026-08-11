"""Authenticated control-plane endpoints: tools, tasks, providers,
audit, inventory."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAuth, client_ip, require_auth, require_csrf
from sqlalchemy import select

from app.db.models import AuditEvent, ProviderConfiguration, WatcherRun
from app.db.session import get_db
from app.services import rate_limit, runbooks
from app.services.approvals_service import (
    ApprovalError,
    approval_public,
    decide_approval,
    list_approvals,
    request_approval,
)
from app.services.audit import read_audit
from app.services.capability_observations import collect_capability_observations
from app.services.fixer_dispatch import assign_and_dispatch_fixer
from app.services.inventory import list_dependencies, list_hosts
from app.services.mcp_clients import (
    McpClientError,
    consume_pairing,
    forget_mcp_client,
    list_mcp_clients,
    list_mcp_pairing_requests,
    mcp_client_public,
    mcp_pairing_public,
    revoke_mcp_client,
    rotate_mcp_client_token,
    set_mcp_client_capabilities,
    start_pairing,
)
from app.services.ops_health import operational_health
from app.services.provider_metadata import watcher_ids_for_provider
from app.services.remediation_workers import RemediationWorkerError, assign_worker_task, worker_job_public
from app.services.provider_definitions import list_provider_definitions
from app.services.task_context import compile_task_context
from app.services.task_router_queue import enqueue_task_routing
from app.services.topology import build_topology
from app.services.topology_snapshot import get_topology_snapshot
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
    create_provider_task,
    create_task,
    get_task,
    handoff_operator_task_to_client,
    list_tasks,
    release_task,
    reopen_task,
    resolve_finding,
    set_status,
    skip_check,
    task_detail,
    task_public,
    task_resolution_labels,
    task_router_statuses,
    update_summary,
)
from app.providers.registry import get_provider, provider_health_snapshot
from app.tools.execution import execute_tool
from app.tools.registry import list_tools

router = APIRouter(prefix="/api", tags=["control"])

_PROVIDER_RUNBOOK_HINTS = {
    "adguard": "dns_alert",
    "cloudflaretunnel": "connectivity_alert",
    "opnsense": "gateway_alert / connectivity_alert",
    "pbs": "backup_alert",
    "uptimekuma": "watcher-created task context",
    "vps": "connectivity_alert",
}

_HTTP_STATUS_BY_ERROR = {
    "unknown_tool": 404,
    "tool_disabled": 403,
    "writes_disabled": 403,
    "policy_denied": 403,
    "approval_required": 403,
    "invalid_input": 400,
    "unauthorized": 401,
    "provider_timeout": 504,
    "provider_error": 502,
    "not_task_owner": 403,
    "task_not_active": 409,
}


class RunToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: dict = Field(default_factory=dict)
    task_id: str | None = None
    approval_id: str | None = None


class ApprovalRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: str = Field(min_length=1, max_length=128)
    input: dict = Field(default_factory=dict)
    task_id: str | None = None


class ApprovalDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool


_APPROVAL_HTTP_STATUS = {
    "unknown_tool": 404,
    "unknown_approval": 404,
    "not_approvable": 400,
    "invalid_input": 400,
}


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    goal: str = Field(default="", max_length=4000)


class ProviderTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=1000)


class ClaimTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=80)


class VersionedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=1)


class ReleaseTaskRequest(VersionedRequest):
    handoff_summary: str = Field(default="", max_length=4000)


class StatusRequest(VersionedRequest):
    status: str = Field(min_length=1, max_length=32)


class SummaryRequest(VersionedRequest):
    summary: str = Field(default="", max_length=8000)


class NoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=4000)


class OperatorHandoffRequest(VersionedRequest):
    client_id: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=4000)


class OperatorCompleteRequest(VersionedRequest):
    note: str = Field(min_length=1, max_length=4000)


class AssignWorkerRequest(VersionedRequest):
    client_id: str = Field(min_length=1, max_length=64)


class FindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str = Field(min_length=1, max_length=16)
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=4000)
    tool_invocation_id: str | None = Field(default=None, max_length=64)


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=512)


class SkipCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


class RevokeMcpClientRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="operator_revoked", max_length=256)


class McpClientCapabilitiesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: list[str] = Field(default_factory=list, max_length=8)
    confirm_worker_conversion: bool = False


class McpPairingStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=32)
    client_label: str = Field(min_length=1, max_length=128)
    host_fingerprint: str = Field(min_length=1, max_length=128)


class McpPairingConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=8, max_length=64)
    pairing_secret: str = Field(min_length=16, max_length=256)


def _task_service_http_error(exc: TaskServiceError) -> HTTPException:
    status = {
        "unknown_task": 404,
        "unknown_client": 404,
        "unknown_finding": 404,
        "unknown_check": 404,
        "version_conflict": 409,
        "task_already_claimed": 409,
        "client_offline": 409,
        "worker_client_requires_assignment": 409,
        "invalid_transition": 409,
        "invalid_status": 400,
        "invalid_input": 400,
        "not_task_owner": 403,
        "not_operator_owner": 403,
    }.get(exc.code, 400)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


def _mcp_client_http_error(exc: McpClientError) -> HTTPException:
    status = {
        "unknown_client": 404,
        "unknown_pairing": 404,
        "client_revoked": 409,
        "client_not_revoked": 409,
        "client_has_worker_history": 409,
        "invalid_agent": 400,
        "invalid_capability": 400,
        "worker_conversion_confirmation_required": 409,
        "invalid_pairing_secret": 403,
        "pairing_consumed": 409,
    }.get(exc.code, 400)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


def _worker_http_error(exc: RemediationWorkerError) -> HTTPException:
    status = {
        "unknown_worker_job": 404,
        "worker_capability_required": 403,
        "worker_client_revoked": 409,
        "worker_job_not_ready": 409,
        "worker_lease_conflict": 409,
        "idempotency_conflict": 409,
    }.get(exc.code, 400)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


@router.get("/tools")
async def tools(auth: CurrentAuth = Depends(require_auth)) -> list[dict]:
    return [tool.public_dict() for tool in list_tools()]


@router.get("/inventory/dependencies")
async def inventory_dependencies(auth: CurrentAuth = Depends(require_auth)) -> list[dict]:
    return [node.model_dump() for node in list_dependencies()]


@router.get("/topology")
async def topology_endpoint(auth: CurrentAuth = Depends(require_auth)) -> dict:
    observation = await execute_tool(
        "proxmox.topology",
        {},
        auth.actor,
        source="rest",
    )
    error = ""
    if not observation.ok and observation.error:
        error = f"Live Proxmox topology unavailable: {observation.error.message}"
    graph = build_topology(observation.result if observation.ok else None, observation_error=error)
    return graph.model_dump(mode="json")


@router.get("/topology/snapshot")
async def topology_snapshot_endpoint(
    force: bool = Query(default=False),
    auth: CurrentAuth = Depends(require_auth),
) -> dict:
    return await get_topology_snapshot(auth.actor, force=force)


async def _capability_observations(auth: CurrentAuth, provider_id: str | None = None) -> list[dict]:
    return await collect_capability_observations(
        auth.actor, provider_id=provider_id, source="rest"
    )


@router.get("/observations")
async def capability_observations_endpoint(
    auth: CurrentAuth = Depends(require_auth),
) -> list[dict]:
    return await _capability_observations(auth)


@router.get("/provider-definitions")
async def provider_definitions_endpoint(
    auth: CurrentAuth = Depends(require_auth),
) -> list[dict]:
    return [definition.model_dump(mode="json") for definition in list_provider_definitions()]


@router.get("/providers/{provider_id}/observations")
async def provider_capability_observations_endpoint(
    provider_id: str,
    auth: CurrentAuth = Depends(require_auth),
) -> list[dict]:
    return await _capability_observations(auth, provider_id)


@router.get("/mcp/clients")
async def mcp_clients_endpoint(
    auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return [mcp_client_public(client) for client in await list_mcp_clients(db)]


@router.get("/mcp/pairing/requests")
async def mcp_pairing_requests_endpoint(
    limit: int = Query(default=25, ge=1, le=100),
    auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return [mcp_pairing_public(request) for request in await list_mcp_pairing_requests(db, limit=limit)]


@router.post("/mcp/pairing/start", status_code=201)
async def start_mcp_pairing_endpoint(
    payload: McpPairingStartRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    key = client_ip(request) or "unknown"
    if not rate_limit.check("mcp.pairing.start", key):
        raise HTTPException(status_code=429, detail="too many MCP pairing requests")
    try:
        pairing = await start_pairing(
            db,
            agent_id=payload.agent_id,
            client_label=payload.client_label,
            host_fingerprint=payload.host_fingerprint,
        )
    except McpClientError as exc:
        await db.rollback()
        raise _mcp_client_http_error(exc) from exc
    await db.commit()
    return {
        "request_id": pairing.request.id,
        "pairing_secret": pairing.pairing_secret,
        "status": pairing.request.status,
        "delivery_status": pairing.request.delivery_status,
        "expires_at": pairing.request.expires_at,
    }


@router.post("/mcp/pairing/consume")
async def consume_mcp_pairing_endpoint(
    payload: McpPairingConsumeRequest,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        result = await consume_pairing(
            db,
            request_id=payload.request_id,
            pairing_secret=payload.pairing_secret,
        )
    except McpClientError as exc:
        await db.commit()
        if exc.code == "pairing_not_approved":
            return JSONResponse(
                status_code=202,
                content={"ok": False, "error": {"code": exc.code, "message": exc.message}},
            )
        raise _mcp_client_http_error(exc) from exc
    await db.commit()
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(
            {"ok": True, "token": result.token, "client": mcp_client_public(result.client)}
        ),
    )


@router.post("/mcp/clients/{client_id}/revoke")
async def revoke_mcp_client_endpoint(
    client_id: str,
    payload: RevokeMcpClientRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        client = await revoke_mcp_client(
            db,
            client_id=client_id,
            reason=payload.reason,
            actor=auth.actor,
        )
    except McpClientError as exc:
        await db.rollback()
        raise _mcp_client_http_error(exc) from exc
    await db.commit()
    return mcp_client_public(client)


@router.post("/mcp/clients/{client_id}/rotate")
async def rotate_mcp_client_endpoint(
    client_id: str,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        result = await rotate_mcp_client_token(db, client_id=client_id, actor=auth.actor)
    except McpClientError as exc:
        await db.rollback()
        raise _mcp_client_http_error(exc) from exc
    await db.commit()
    return {"ok": True, "token": result.token, "client": mcp_client_public(result.client)}


@router.put("/mcp/clients/{client_id}/capabilities")
async def update_mcp_client_capabilities_endpoint(
    client_id: str,
    payload: McpClientCapabilitiesRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        client = await set_mcp_client_capabilities(
            db,
            client_id=client_id,
            capabilities=payload.capabilities,
            actor=auth.actor,
            confirm_worker_conversion=payload.confirm_worker_conversion,
        )
    except McpClientError as exc:
        await db.rollback()
        raise _mcp_client_http_error(exc) from exc
    await db.commit()
    return mcp_client_public(client)


@router.delete("/mcp/clients/{client_id}", status_code=204)
async def forget_mcp_client_endpoint(
    client_id: str,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        await forget_mcp_client(db, client_id=client_id, actor=auth.actor)
    except McpClientError as exc:
        await db.rollback()
        raise _mcp_client_http_error(exc) from exc
    await db.commit()
    return Response(status_code=204)


@router.post("/tools/{tool_id}/run")
async def run_tool(
    tool_id: str,
    payload: RunToolRequest,
    auth: CurrentAuth = Depends(require_csrf),
) -> JSONResponse:
    result = await execute_tool(
        tool_id,
        payload.input,
        auth.actor,
        task_id=payload.task_id,
        approval_id=payload.approval_id,
        source="rest",
    )
    status_code = 200 if result.ok else _HTTP_STATUS_BY_ERROR.get(
        result.error.code if result.error is not None else "", 500
    )
    return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"))


@router.post("/approvals", status_code=201)
async def request_approval_endpoint(
    payload: ApprovalRequestPayload,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        approval = await request_approval(
            db,
            tool_id=payload.tool_id,
            raw_input=payload.input,
            actor=auth.actor,
            task_id=payload.task_id,
            source="rest",
        )
    except ApprovalError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=_APPROVAL_HTTP_STATUS.get(exc.code, 400), detail=exc.message
        ) from exc
    result = approval_public(approval)
    await db.commit()
    return jsonable_encoder(result)


@router.get("/approvals")
async def list_approvals_endpoint(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    approvals = await list_approvals(db, status=status, limit=limit)
    return [jsonable_encoder(approval_public(approval)) for approval in approvals]


@router.post("/approvals/{approval_id}/decide")
async def decide_approval_endpoint(
    approval_id: str,
    payload: ApprovalDecisionPayload,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        approval, outcome = await decide_approval(
            db,
            approval_id=approval_id,
            approve=payload.approve,
            actor=auth.actor,
            source="rest",
        )
    except ApprovalError as exc:
        # Keep the audit row recording the unknown-id attempt.
        await db.commit()
        raise HTTPException(status_code=404, detail=exc.message) from exc
    result = {**approval_public(approval), "outcome": outcome}
    await db.commit()
    return jsonable_encoder(result)


@router.get("/tasks")
async def tasks(
    status: str | None = Query(default=None),
    assigned_agent: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
    auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    try:
        rows = await list_tasks(db, status=status, assigned_agent=assigned_agent, limit=limit)
    except TaskServiceError as exc:
        raise _task_service_http_error(exc) from exc
    task_ids = [task.id for task in rows]
    router_statuses = await task_router_statuses(db, task_ids)
    resolution_labels = await task_resolution_labels(db, task_ids)
    return [
        task_public(
            task,
            router_status=router_statuses.get(task.id, ""),
            resolution_label=resolution_labels.get(task.id, ""),
        )
        for task in rows
    ]


@router.post("/tasks", status_code=201)
async def create_task_endpoint(
    payload: CreateTaskRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task = await create_task(db, payload.title, payload.goal, auth.actor)
        await enqueue_task_routing(
            db,
            task,
            auth.actor,
            source="rest",
            context={"trigger": "manual", "title": payload.title, "goal": payload.goal},
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return task_public(task)


@router.get("/tasks/{task_id}")
async def task_detail_endpoint(
    task_id: str,
    auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict:
    task = await get_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="unknown task")
    return await task_detail(db, task)


@router.get("/tasks/{task_id}/context")
async def task_context_endpoint(
    task_id: str,
    auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await compile_task_context(db, task_id)
    except TaskServiceError as exc:
        raise _task_service_http_error(exc) from exc


@router.post("/tasks/{task_id}/claim")
async def claim_task_endpoint(
    task_id: str,
    payload: ClaimTaskRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task = await claim_task(db, task_id, payload.agent_id, auth.actor, source="rest")
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return task_public(task)


@router.post("/tasks/{task_id}/claim-self")
async def claim_task_as_operator_endpoint(
    task_id: str,
    payload: VersionedRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task = await claim_task_as_operator(
            db,
            task_id,
            auth.actor,
            expected_version=payload.expected_version,
            source="rest",
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return task_public(task)


@router.post("/tasks/{task_id}/assign-worker", status_code=201)
async def assign_worker_task_endpoint(
    task_id: str,
    payload: AssignWorkerRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task, job = await assign_worker_task(
            db,
            task_id=task_id,
            client_id=payload.client_id,
            actor=auth.actor,
            expected_version=payload.expected_version,
            source="rest",
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    except RemediationWorkerError as exc:
        await db.rollback()
        raise _worker_http_error(exc) from exc
    await db.commit()
    return {"task": task_public(task), "job": worker_job_public(job)}


@router.post("/tasks/{task_id}/handoff-to-client")
async def handoff_operator_task_endpoint(
    task_id: str,
    payload: OperatorHandoffRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task = await handoff_operator_task_to_client(
            db,
            task_id,
            payload.client_id,
            payload.note,
            auth.actor,
            expected_version=payload.expected_version,
            source="rest",
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return task_public(task)


@router.post("/tasks/{task_id}/complete-as-operator")
async def complete_task_as_operator_endpoint(
    task_id: str,
    payload: OperatorCompleteRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task = await complete_task_as_operator(
            db,
            task_id,
            payload.note,
            auth.actor,
            expected_version=payload.expected_version,
            source="rest",
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return task_public(task, resolution_label="human_handled")


@router.post("/tasks/{task_id}/dispatch-fixer")
async def dispatch_task_to_fixer_endpoint(
    task_id: str,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task, dispatch = await assign_and_dispatch_fixer(
            db,
            task_id,
            auth.actor,
            source="rest",
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    return {"task": task_public(task), "dispatch": dispatch.public()}


@router.post("/tasks/{task_id}/release")
async def release_task_endpoint(
    task_id: str,
    payload: ReleaseTaskRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task = await release_task(
            db,
            task_id,
            auth.actor,
            expected_version=payload.expected_version,
            handoff_summary=payload.handoff_summary,
            source="rest",
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return task_public(task)


@router.post("/tasks/{task_id}/status")
async def set_task_status_endpoint(
    task_id: str,
    payload: StatusRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task = await set_status(
            db,
            task_id,
            payload.status,
            auth.actor,
            expected_version=payload.expected_version,
            source="rest",
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return task_public(task)


@router.patch("/tasks/{task_id}/summary")
async def update_task_summary_endpoint(
    task_id: str,
    payload: SummaryRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task = await update_summary(
            db,
            task_id,
            payload.summary,
            auth.actor,
            expected_version=payload.expected_version,
            source="rest",
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return task_public(task)


@router.post("/tasks/{task_id}/notes", status_code=201)
async def add_task_note_endpoint(
    task_id: str,
    payload: NoteRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        event = await add_note(db, task_id, payload.note, auth.actor, source="rest")
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return {"id": event.id, "kind": event.kind, "payload": event.payload, "created_at": event.created_at}


@router.post("/tasks/{task_id}/findings", status_code=201)
async def add_task_finding_endpoint(
    task_id: str,
    payload: FindingRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services.tasks_service import finding_public

    try:
        finding = await add_finding(
            db,
            task_id,
            payload.severity,
            payload.title,
            payload.description,
            auth.actor,
            source="rest",
            tool_invocation_id=payload.tool_invocation_id,
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return finding_public(finding)


@router.post("/tasks/{task_id}/findings/{finding_id}/resolve")
async def resolve_task_finding_endpoint(
    task_id: str,
    finding_id: str,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services.tasks_service import finding_public

    try:
        finding = await resolve_finding(db, task_id, finding_id, auth.actor, source="rest")
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return finding_public(finding)


@router.post("/tasks/{task_id}/checks", status_code=201)
async def add_task_check_endpoint(
    task_id: str,
    payload: CheckRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services.tasks_service import check_public

    try:
        check = await add_check(db, task_id, payload.description, auth.actor, source="rest")
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return check_public(check)


@router.post("/tasks/{task_id}/checks/{check_id}/complete")
async def complete_task_check_endpoint(
    task_id: str,
    check_id: str,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services.tasks_service import check_public

    try:
        check = await complete_check(db, task_id, check_id, auth.actor, source="rest")
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return check_public(check)


@router.post("/tasks/{task_id}/checks/{check_id}/skip")
async def skip_task_check_endpoint(
    task_id: str,
    check_id: str,
    payload: SkipCheckRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services.tasks_service import check_public

    try:
        check = await skip_check(db, task_id, check_id, auth.actor, payload.reason, source="rest")
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return check_public(check)


@router.post("/tasks/{task_id}/complete")
async def complete_task_endpoint(
    task_id: str,
    payload: VersionedRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task = await complete_task(
            db, task_id, auth.actor, expected_version=payload.expected_version, source="rest"
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return task_public(task)


@router.post("/tasks/{task_id}/reopen")
async def reopen_task_endpoint(
    task_id: str,
    payload: VersionedRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task = await reopen_task(
            db, task_id, auth.actor, expected_version=payload.expected_version, source="rest"
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return task_public(task)


@router.get("/providers")
async def providers(
    auth: CurrentAuth = Depends(require_auth), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    snapshots = await provider_health_snapshot(db)
    await db.commit()
    result = []
    for snapshot in snapshots:
        provider = get_provider(snapshot.provider_id)
        configuration = await db.get(ProviderConfiguration, snapshot.provider_id)
        data = snapshot.model_dump(mode="json")
        last_error = None
        if configuration and configuration.last_error_at:
            last_error = {
                "status": configuration.last_error_status,
                "message": configuration.last_error_detail,
                "at": configuration.last_error_at.isoformat(),
            }
        result.append(
            {
                "id": snapshot.provider_id,
                "name": provider.display_name if provider else snapshot.provider_id,
                "status": data["status"],
                "detail": data["detail"],
                "last_ok_at": data["last_ok_at"],
                "checked_at": data["checked_at"],
                "tool_count": len(provider.capabilities()) if provider else 0,
                "watchers": watcher_ids_for_provider(snapshot.provider_id),
                "last_error": last_error,
            }
        )
    return result


@router.post("/providers/{provider_id}/task", status_code=201)
async def create_provider_task_endpoint(
    provider_id: str,
    payload: ProviderTaskRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    snapshots = await provider_health_snapshot(db)
    snapshot = next((item for item in snapshots if item.provider_id == provider_id), None)
    if snapshot is None:
        await db.rollback()
        raise HTTPException(status_code=404, detail="unknown provider")
    provider = get_provider(snapshot.provider_id)
    provider_name = provider.display_name if provider else snapshot.provider_id
    runbook_hint = _PROVIDER_RUNBOOK_HINTS.get(snapshot.provider_id, "task context fallback")
    snapshot_data = snapshot.model_dump(mode="json")
    detail = snapshot.detail or "No provider detail reported."
    title = f"Check provider {provider_name}"
    goal_lines = [
        f"Provider {snapshot.provider_id} is {snapshot.status}.",
        f"Detail: {detail}",
        f"Suggested runbook: {runbook_hint}.",
        "Use read-only tools only, collect evidence in findings/checks, and do not attempt remediation.",
    ]
    note = payload.note.strip()
    if note:
        goal_lines.append(f"Operator note: {note}")
    context = {
        "provider_id": snapshot.provider_id,
        "provider_name": provider_name,
        "status": snapshot.status,
        "detail": snapshot.detail,
        "last_ok_at": snapshot_data["last_ok_at"],
        "checked_at": snapshot_data["checked_at"],
        "runbook_hint": runbook_hint,
        "operator_note": note,
    }
    try:
        task = await create_provider_task(
            db,
            title,
            "\n".join(goal_lines),
            auth.actor,
            provider_context=context,
        )
        await enqueue_task_routing(db, task, auth.actor, source="provider", context=context)
    except TaskServiceError as exc:
        await db.rollback()
        raise _task_service_http_error(exc) from exc
    await db.commit()
    return task_public(task)


@router.get("/runbooks")
async def runbook_list(auth: CurrentAuth = Depends(require_auth)) -> list[dict]:
    return [
        {
            "incident_type": item.incident_type,
            "label": item.label,
            "steps": [
                {"tool_id": step.tool_id, "evidence": step.evidence}
                for step in item.steps
            ],
            "escalation_note": item.escalation_note,
        }
        for item in runbooks.list_runbooks()
    ]


@router.get("/ops/errors")
async def ops_errors(
    limit: int = Query(default=20, ge=1, le=50),
    auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    audit_rows = (
        await db.execute(
            select(AuditEvent)
            .where(AuditEvent.outcome.notin_(["success", "ok"]))
            .order_by(AuditEvent.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    watcher_rows = (
        await db.execute(
            select(WatcherRun)
            .where(WatcherRun.status == "error")
            .order_by(WatcherRun.started_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    rows = [
        {
            "id": row.id,
            "created_at": row.created_at,
            "source": row.source,
            "kind": "audit",
            "title": row.action,
            "detail": row.outcome,
            "tool_id": row.tool_id,
            "task_id": row.task_id,
        }
        for row in audit_rows
    ] + [
        {
            "id": row.id,
            "created_at": row.started_at,
            "source": "watcher",
            "kind": "watcher",
            "title": row.watcher_id,
            "detail": row.error or row.status,
            "tool_id": "",
            "task_id": "",
        }
        for row in watcher_rows
    ]
    rows.sort(key=lambda item: item["created_at"], reverse=True)
    return rows[:limit]


@router.get("/ops/health")
async def ops_health(
    auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await operational_health(db)


@router.get("/audit")
async def audit(
    limit: int = 100,
    auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    events = await read_audit(db, limit=limit)
    return [
        {
            "id": event.id,
            "created_at": event.created_at,
            "actor": f"{event.actor_kind}:{event.actor_id}",
            "source": event.source,
            "action": event.action,
            "tool_id": event.tool_id,
            "task_id": event.task_id,
            "outcome": event.outcome,
            "duration_ms": event.duration_ms,
            "metadata": event.meta,
        }
        for event in events
    ]


@router.get("/inventory/hosts")
async def inventory_hosts(auth: CurrentAuth = Depends(require_auth)) -> list[dict]:
    return [host.model_dump() for host in list_hosts()]
