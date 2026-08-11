"""Shared tool execution core.

Every interface (REST, MCP, Telegram) calls execute_tool(); nothing else may
invoke a provider. The pipeline: lookup → enabled → input validation →
authentication → risk policy → approval → timeout → provider invocation →
normalization → redaction → audit → task event → response.
"""

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import or_, update

from app.db.models import Approval, Task, TaskEvent, ToolInvocation, utcnow
from app.db.session import get_session_factory
from app.domain.actors import Actor
from app.providers.errors import ProviderError
from app.services.approvals_service import input_digest
from app.services.audit import write_audit
from app.services.inventory import medium_risk_allowlist
from app.services.redaction import redact
from app.services.remediation_workers import RemediationWorkerError, validate_worker_lease
from app.services.tasks_service import FINAL_TASK_STATUSES, agent_identity
from app.tools.governance import APPROVED_WRITE_TOOLS
from app.tools.registry import ToolDefinition, get_tool


class ExecutionError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    invocation_id: str
    tool_id: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    result: dict | None = None
    error: ExecutionError | None = None
    # Present whenever a task_id was passed and the task exists, whether the
    # call succeeded or was rejected (e.g. not_task_owner) — lets a caller
    # chain further task-service calls with the right expected_version
    # without an extra tasks.get round trip.
    task_version: int | None = None


_SOURCE_BY_ACTOR_KIND = {"user": "rest", "service": "mcp", "agent": "mcp", "telegram": "telegram"}


def _sanitize_validation_error(exc: ValidationError) -> str:
    """Describe validation failures without echoing submitted values."""
    parts = []
    for issue in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(item) for item in issue.get("loc", ())) or "input"
        parts.append(f"{location}: {issue.get('type', 'invalid')}")
    return "; ".join(parts[:10])


async def execute_tool(
    tool_id: str,
    raw_input: dict | None,
    actor: Actor,
    task_id: str | None = None,
    approval_id: str | None = None,
    source: str | None = None,
    worker_client_id: str | None = None,
    worker_job_id: str | None = None,
    worker_lease_token: str | None = None,
) -> ExecutionResult:
    started = datetime.now(UTC)
    invocation_id = str(uuid4())
    source = source or _SOURCE_BY_ACTOR_KIND.get(actor.kind, "system")
    tool = get_tool(tool_id)

    error: ExecutionError | None = None
    result: dict | None = None
    validated: BaseModel | None = None
    approval_consumed = False

    # 1-2. Lookup and enablement.
    if tool is None:
        error = ExecutionError(code="unknown_tool", message=f"unknown tool: {tool_id}")
    elif not tool.enabled:
        error = ExecutionError(code="tool_disabled", message="tool is disabled")

    # 3. Input validation (strict, unknown fields rejected).
    if error is None and tool is not None:
        try:
            validated = tool.input_model.model_validate(raw_input or {})
        except ValidationError as exc:
            error = ExecutionError(code="invalid_input", message=_sanitize_validation_error(exc))

    # Successful lookup and validation establish these invariants for the
    # remaining policy and execution pipeline.
    if error is None:
        assert tool is not None
        assert validated is not None
    active_tool = cast(ToolDefinition, tool)
    validated_input = cast(BaseModel, validated)

    # 4. Authentication.
    if error is None and actor.kind not in ("user", "service", "agent", "telegram"):
        error = ExecutionError(code="unauthorized", message="actor is not authenticated")

    # 5. Risk policy.
    if error is None:
        if active_tool.mode == "write" and active_tool.id not in APPROVED_WRITE_TOOLS:
            error = ExecutionError(
                code="writes_disabled",
                message="write tool has no operator-approved capability decision",
            )
        elif active_tool.risk == "critical":
            error = ExecutionError(code="tool_disabled", message="critical tools are not implemented")
        elif active_tool.risk == "medium" and active_tool.id not in medium_risk_allowlist():
            error = ExecutionError(
                code="policy_denied",
                message="medium-risk tool requires explicit policy allowlisting",
            )

    async with get_session_factory()() as db:
        task: Task | None = None
        attach_task_event = False
        worker_actor = actor.kind == "agent" and actor.id.startswith("worker:")
        worker_job = None
        if error is None and worker_actor:
            if not task_id or not worker_client_id or not worker_job_id or not worker_lease_token:
                error = ExecutionError(
                    code="invalid_worker_lease",
                    message="worker tool execution requires task and lease metadata",
                )
            else:
                try:
                    worker_job = await validate_worker_lease(
                        db,
                        task_id=task_id,
                        worker_job_id=worker_job_id,
                        worker_lease_token=worker_lease_token,
                        client_id=worker_client_id,
                    )
                    if actor.id != worker_job.principal_id:
                        raise RemediationWorkerError(
                            "invalid_worker_lease", "worker lease is not valid"
                        )
                except RemediationWorkerError as exc:
                    error = ExecutionError(code=exc.code, message=exc.message)
        if error is None and task_id:
            task = await db.get(Task, task_id, with_for_update=True)
            if task is None:
                error = ExecutionError(code="invalid_input", message="unknown task_id")
            elif task.status in FINAL_TASK_STATUSES:
                error = ExecutionError(
                    code="task_not_active",
                    message=f"task is {task.status}; reopen it before running tools",
                )
            else:
                owner = agent_identity(actor)
                if owner is not None and task.assigned_agent and task.assigned_agent != owner:
                    error = ExecutionError(
                        code="not_task_owner", message="task is assigned to another agent"
                    )
                else:
                    attach_task_event = True

        # 6. Approval verification (every write tool and every high-risk
        # tool). The conditional UPDATE is the consume operation: concurrent
        # callers cannot both move the same row from approved to consumed,
        # even if they read it at the same time. Keep this after task
        # validation so an invalid task does not burn an otherwise valid
        # approval. An input-bound approval (input_hash set) only unlocks
        # the exact validated input it was requested for.
        if error is None and (active_tool.mode == "write" or active_tool.risk == "high"):
            if not approval_id:
                error = ExecutionError(code="approval_required", message="approval required")
            else:
                task_scope = Approval.task_id.is_(None)
                if task_id:
                    task_scope = or_(task_scope, Approval.task_id == task_id)
                digest = input_digest(validated_input)
                approval_conditions = [
                    Approval.id == approval_id,
                    Approval.status == "approved",
                    Approval.consumed_at.is_(None),
                    or_(Approval.tool_id == "", Approval.tool_id == active_tool.id),
                    or_(Approval.input_hash == "", Approval.input_hash == digest),
                    task_scope,
                    Approval.expires_at > utcnow(),
                ]
                if worker_actor:
                    assert worker_job is not None
                    approval_conditions.extend(
                        [
                            Approval.requested_by == actor.audit_id(),
                            Approval.worker_job_id == worker_job.id,
                            Approval.worker_lease_generation == worker_job.lease_generation,
                        ]
                    )
                claimed = await db.execute(
                    update(Approval)
                    .where(*approval_conditions)
                    .values(status="consumed", consumed_at=utcnow())
                    .returning(Approval.id)
                )
                approval_consumed = claimed.scalar_one_or_none() is not None
                if not approval_consumed:
                    error = ExecutionError(
                        code="approval_required", message="approval invalid or expired"
                    )

        # 7-9. Execution with timeout, provider invocation, normalization.
        if error is None:
            try:
                runner = active_tool.runner
                if runner is None:
                    raise TypeError("tool runner is not configured")
                result = await asyncio.wait_for(runner(validated_input), active_tool.timeout_seconds)
                if active_tool.output_model is not None:
                    normalized = active_tool.output_model.model_validate(result)
                    result = normalized.model_dump(mode="json")
            except asyncio.TimeoutError:
                error = ExecutionError(code="provider_timeout", message="tool execution timed out")
            except ValidationError:
                error = ExecutionError(
                    code="provider_error", message="provider returned an invalid result"
                )
            except ProviderError as exc:
                code = "provider_timeout" if exc.code == "timeout" else "provider_error"
                error = ExecutionError(code=code, message=f"{exc.code}: {exc.message}")
            except Exception as exc:  # never leak stack traces or secrets
                error = ExecutionError(
                    code="provider_error", message=f"unexpected failure: {exc.__class__.__name__}"
                )

        finished = datetime.now(UTC)
        duration_ms = int((finished - started).total_seconds() * 1000)

        # 10. Redaction of everything we persist or return.
        provider_id = tool.provider_id if tool else ""
        safe_input = redact(raw_input or {}, provider_id=provider_id or None)
        safe_result = redact(result, provider_id=provider_id or None) if result is not None else None

        # 11. Audit.
        await write_audit(
            db,
            actor=actor,
            source=source,
            action="tool.run",
            outcome="success" if error is None else error.code,
            tool_id=tool_id,
            task_id=task_id or "",
            request_id=invocation_id,
            duration_ms=duration_ms,
            metadata={"input": safe_input, "error": error.message if error else None},
            provider_id=provider_id or None,
        )

        # 12. Invocation + task-event persistence.
        invocation = ToolInvocation(
            id=invocation_id,
            task_id=task.id if task else None,
            actor_kind=actor.kind,
            actor_id=actor.id,
            tool_id=tool_id,
            provider_id=provider_id,
            request_id=invocation_id,
            input_redacted=safe_input,
            status="success" if error is None else "error",
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
            error_code=error.code if error else "",
            error_message=error.message if error else "",
            approval_id=approval_id if approval_consumed else None,
        )
        db.add(invocation)

        if attach_task_event and task is not None:
            db.add(
                TaskEvent(
                    task_id=task.id,
                    kind="task.tool_invoked",
                    payload={
                        "invocation_id": invocation_id,
                        "tool_id": tool_id,
                        "status": "success" if error is None else "error",
                        "duration_ms": duration_ms,
                    },
                )
            )
            task.last_activity_at = utcnow()
            task.updated_at = utcnow()
            task.version += 1

        await db.commit()

    # 13. Response.
    return ExecutionResult(
        ok=error is None,
        invocation_id=invocation_id,
        tool_id=tool_id,
        started_at=started,
        finished_at=finished,
        duration_ms=duration_ms,
        result=safe_result,
        error=error,
        task_version=task.version if task is not None else None,
    )
