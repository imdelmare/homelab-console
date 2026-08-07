"""Lightweight AI routing for newly created tasks.

The router can enrich a task with category/priority/owner/runbook suggestions,
but it never claims tasks, executes tools, closes incidents, or changes
infrastructure state. The backend stores the model decision as a task event for
operator/agent review.
"""

from __future__ import annotations

import json
import asyncio
import logging
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import Task, TaskEvent
from app.domain.actors import Actor
from app.services.ai_manager import (
    AIManagerError,
    ai_manager_available,
    mark_ai_manager_available,
    mark_ai_manager_unavailable,
    request_ai_manager,
)
from app.services.audit import write_audit
from app.services.luna_metrics import record_llm_usage
from app.services.luna_notifications import notify_router_failure, notify_router_recovery
from app.services.opencode_go import OpenCodeGoError, request_structured_decision
from app.services.redaction import redact

logger = logging.getLogger("homelab.task_router")
TASK_ROUTER_PROMPT_VERSION = "task-router-v1"
TASK_ROUTER_SCHEMA_VERSION = "task-router-decision-v1"

TASK_ROUTER_CATEGORIES = {
    "network",
    "dns",
    "backup",
    "security",
    "provider",
    "watcher",
    "mcp",
    "ui",
    "docs",
    "automation",
    "unknown",
}
TASK_ROUTER_PRIORITIES = {"low", "medium", "high", "urgent"}
TASK_ROUTER_SEVERITIES = {"info", "warning", "critical"}
TASK_ROUTER_OWNERS = {"claude", "fixer", "codex", "cline", "opencode", "operator", "none"}
TASK_ROUTER_ACTIONS = {"keep", "merge_candidate", "operator_review"}


class TaskRouterError(Exception):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class TaskRouterDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(max_length=32)
    category: str = Field(max_length=32)
    priority: str = Field(max_length=16)
    severity: str = Field(max_length=16)
    suggested_owner: str = Field(max_length=16)
    runbook: str | None = Field(default=None, max_length=128)
    dedupe_candidate: str | None = Field(default=None, max_length=128)
    summary: str = Field(default="", max_length=1200)
    first_steps: list[str] = Field(default_factory=list, max_length=5)
    labels: list[str] = Field(default_factory=list, max_length=8)
    needs_operator: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class TaskRouterResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: TaskRouterDecision
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    telemetry: dict[str, Any] = Field(default_factory=dict)


class TaskRouterModel(Protocol):
    async def decide(self, context: dict[str, Any]) -> TaskRouterResult:
        ...


class OpenAITaskRouterModel:
    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.openai_api_key
        self.model = settings.task_router_model or settings.conversation_model

    async def decide(self, context: dict[str, Any]) -> TaskRouterResult:
        settings = get_settings()
        if not self.api_key:
            raise TaskRouterError("task router model is not configured")

        payload = {
            "model": self.model,
            "reasoning": {"effort": settings.task_router_reasoning_effort},
            "instructions": _instructions(),
            "input": [{"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "task_router_decision",
                    "strict": True,
                    "schema": _task_router_schema(),
                }
            },
            "max_output_tokens": settings.task_router_max_output_tokens,
            "store": False,
        }
        response = await _post_openai_response(
            api_key=self.api_key,
            payload=payload,
            timeout_seconds=settings.task_router_timeout_seconds,
        )
        if response.status_code >= 400:
            raise TaskRouterError(
                "task router model request failed",
                details={"http_status": response.status_code},
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise TaskRouterError(
                "task router model returned an invalid response",
                details={"http_status": response.status_code},
            ) from exc
        if not isinstance(body, dict):
            raise TaskRouterError(
                "task router model returned an invalid response",
                details={"http_status": response.status_code},
            )
        output_text = _extract_output_text(body)
        diagnostics = _response_diagnostics(
            body,
            output_text=output_text,
            http_status=response.status_code,
        )
        if diagnostics.get("response_status") == "incomplete" or diagnostics.get("incomplete_reason"):
            raise TaskRouterError("task router model response incomplete", details=diagnostics)
        try:
            decision = TaskRouterDecision.model_validate_json(output_text)
        except ValidationError as exc:
            raise TaskRouterError(
                "task router model returned invalid structured output",
                details=diagnostics,
            ) from exc
        usage_value = body.get("usage")
        usage: dict[str, Any] = usage_value if isinstance(usage_value, dict) else {}
        input_details_value = usage.get("input_tokens_details")
        input_details: dict[str, Any] = input_details_value if isinstance(input_details_value, dict) else {}
        output_details_value = usage.get("output_tokens_details")
        output_details: dict[str, Any] = output_details_value if isinstance(output_details_value, dict) else {}
        return TaskRouterResult(
            decision=_normalize_decision(decision),
            model=str(body.get("model") or self.model),
            input_tokens=int(usage.get("input_tokens") or 0),
            cached_input_tokens=int(input_details.get("cached_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            reasoning_tokens=int(output_details.get("reasoning_tokens") or 0),
        )


class AIManagerTaskRouterModel:
    async def decide(self, context: dict[str, Any]) -> TaskRouterResult:
        settings = get_settings()
        try:
            result = await request_ai_manager(
                instructions=_instructions(),
                context=context,
                schema_name="task_router_decision",
                schema=_task_router_schema(),
                max_output_tokens=settings.task_router_max_output_tokens,
                timeout_seconds=settings.task_router_timeout_seconds,
                prompt_version=TASK_ROUTER_PROMPT_VERSION,
                schema_version=TASK_ROUTER_SCHEMA_VERSION,
            )
        except AIManagerError as exc:
            raise TaskRouterError(
                str(exc),
                details={"model": settings.ai_manager_model, "telemetry": exc.telemetry},
            ) from exc
        try:
            decision = TaskRouterDecision.model_validate_json(result.output_text)
        except ValidationError as exc:
            raise TaskRouterError(
                "AI manager returned invalid structured output",
                details={
                    "model": result.model,
                    "input_units": result.input_tokens,
                    "cached_input_tokens": result.cached_input_tokens,
                    "output_units": result.output_tokens,
                    "telemetry": {**result.telemetry, "error_kind": "schema_error"},
                },
            ) from exc
        return TaskRouterResult(
            decision=_normalize_decision(decision),
            model=result.model,
            input_tokens=result.input_tokens,
            cached_input_tokens=result.cached_input_tokens,
            output_tokens=result.output_tokens,
            telemetry=result.telemetry,
        )


class AIManagerWithOpenAIFallbackTaskRouterModel:
    def __init__(
        self,
        primary: TaskRouterModel | None = None,
        fallback: TaskRouterModel | None = None,
    ) -> None:
        self.primary = primary or AIManagerTaskRouterModel()
        self.fallback = fallback or OpenAITaskRouterModel()

    async def decide(self, context: dict[str, Any]) -> TaskRouterResult:
        primary_telemetry: dict[str, Any]
        if ai_manager_available():
            try:
                result = await self.primary.decide(context)
            except TaskRouterError as exc:
                mark_ai_manager_unavailable()
                logger.warning("AI manager unavailable; using OpenAI task router fallback: %s", str(exc))
                telemetry_value = exc.details.get("telemetry")
                primary_telemetry = telemetry_value if isinstance(telemetry_value, dict) else {}
            else:
                mark_ai_manager_available()
                return result
        else:
            primary_telemetry = {
                "provider": "ai_manager",
                "fallback_reason": "circuit_open",
                "error_kind": "circuit_open",
                "prompt_version": TASK_ROUTER_PROMPT_VERSION,
                "schema_version": TASK_ROUTER_SCHEMA_VERSION,
                "model_version": get_settings().ai_manager_model,
            }
        try:
            result = await self.fallback.decide(context)
        except TaskRouterError as exc:
            fallback_telemetry_value = exc.details.get("telemetry")
            fallback_telemetry = (
                fallback_telemetry_value if isinstance(fallback_telemetry_value, dict) else {}
            )
            raise TaskRouterError(
                str(exc),
                details={
                    **exc.details,
                    "model": exc.details.get("model")
                    or get_settings().task_router_model
                    or get_settings().conversation_model,
                    "telemetry": {
                        **primary_telemetry,
                        **fallback_telemetry,
                        "provider": "openai",
                        "fallback_used": True,
                        "fallback_reason": str(
                            primary_telemetry.get("error_kind") or "unavailable"
                        ),
                        "error_kind": str(
                            fallback_telemetry.get("error_kind") or "fallback_error"
                        ),
                    },
                },
            ) from exc
        result.telemetry = {
            **primary_telemetry,
            "provider": "openai",
            "fallback_used": True,
            "fallback_reason": str(primary_telemetry.get("error_kind") or "unavailable"),
        }
        return result


class OpenCodeTaskRouterModel:
    """Bounded no-tool router over the fixed OpenCode Go HTTPS API."""

    async def decide(self, context: dict[str, Any]) -> TaskRouterResult:
        settings = get_settings()
        model = settings.opencode_go_router_model
        try:
            result = await request_structured_decision(
                model=model,
                instructions=_instructions(),
                context=context,
                schema_name="task_router_decision",
                schema=_task_router_schema(),
                max_output_tokens=settings.task_router_max_output_tokens,
                timeout_seconds=settings.task_router_timeout_seconds,
            )
        except OpenCodeGoError as exc:
            raise TaskRouterError(
                str(exc),
                details={
                    "http_status": exc.http_status,
                    "transient": exc.transient,
                    "model": f"opencode-go/{model}",
                    "telemetry": {
                        "provider": "opencode_go",
                        "error_kind": exc.error_kind,
                        "prompt_version": TASK_ROUTER_PROMPT_VERSION,
                        "schema_version": TASK_ROUTER_SCHEMA_VERSION,
                        "model_version": f"opencode-go/{model}",
                    },
                },
            ) from exc
        try:
            decision = TaskRouterDecision.model_validate_json(result.output_text)
        except ValidationError as exc:
            raise TaskRouterError(
                "OpenCode Go task router returned invalid structured output",
                details={
                    "model": f"opencode-go/{result.model}",
                    "input_units": result.input_tokens,
                    "cached_input_tokens": result.cached_input_tokens,
                    "output_units": result.output_tokens,
                    "reasoning_tokens": result.reasoning_tokens,
                    "transient": False,
                    "telemetry": {
                        "provider": "opencode_go",
                        "error_kind": "schema_error",
                        "prompt_version": TASK_ROUTER_PROMPT_VERSION,
                        "schema_version": TASK_ROUTER_SCHEMA_VERSION,
                        "model_version": f"opencode-go/{result.model}",
                    },
                },
            ) from exc
        return TaskRouterResult(
            decision=_normalize_decision(decision),
            model=f"opencode-go/{result.model}",
            input_tokens=result.input_tokens,
            cached_input_tokens=result.cached_input_tokens,
            output_tokens=result.output_tokens,
            reasoning_tokens=result.reasoning_tokens,
            telemetry={
                "provider": "opencode_go",
                "prompt_version": TASK_ROUTER_PROMPT_VERSION,
                "schema_version": TASK_ROUTER_SCHEMA_VERSION,
                "model_version": f"opencode-go/{result.model}",
            },
        )


def get_task_router_model() -> TaskRouterModel:
    settings = get_settings()
    provider = settings.task_router_provider.strip().lower() or settings.conversation_provider.strip().lower()
    if provider == "ai_manager":
        return AIManagerWithOpenAIFallbackTaskRouterModel()
    if provider == "opencode_go":
        return OpenCodeTaskRouterModel()
    return OpenAITaskRouterModel()


async def route_task(
    db: AsyncSession,
    task: Task,
    actor: Actor,
    *,
    source: str,
    context: dict[str, Any] | None = None,
    model: TaskRouterModel | None = None,
    raise_on_error: bool = False,
) -> TaskRouterDecision | None:
    settings = get_settings()
    if model is None and not settings.task_router_enabled:
        return None

    adapter = model or get_task_router_model()
    router_context = _router_context(task, source=source, context=context or {})
    try:
        result = await adapter.decide(router_context)
    except Exception as exc:
        if raise_on_error:
            raise
        await record_task_router_failure(
            db,
            task,
            actor,
            source=source,
            context=context or {},
            error=exc,
        )
        return None

    decision = _normalize_decision(result.decision)
    payload = _json_safe(
        redact(
            {
                "model": result.model,
                "input_tokens": result.input_tokens,
                "cached_input_tokens": result.cached_input_tokens,
                "output_tokens": result.output_tokens,
                "reasoning_tokens": result.reasoning_tokens,
                "decision": decision.model_dump(mode="json"),
                "context": router_context,
            }
        )
    )
    event = TaskEvent(task_id=task.id, kind="task.router_decision", payload=payload)
    db.add(event)
    await db.flush()
    await record_llm_usage(
        db,
        component="task_router",
        model=result.model,
        status="success",
        input_tokens=result.input_tokens,
        cached_input_tokens=result.cached_input_tokens,
        output_tokens=result.output_tokens,
        reasoning_tokens=result.reasoning_tokens,
        task_id=task.id,
        reference_id=event.id,
        **result.telemetry,
    )
    await notify_router_recovery(db, model=result.model)
    await write_audit(
        db,
        actor=actor,
        source=source,
        action="task.router_decision",
        outcome="success",
        task_id=task.id,
        metadata=payload,
    )
    return decision


async def record_task_router_failure(
    db: AsyncSession,
    task: Task,
    actor: Actor,
    *,
    source: str,
    context: dict[str, Any],
    error: Exception,
) -> None:
    settings = get_settings()
    error_details = error.details if isinstance(error, TaskRouterError) else {}
    telemetry_value = error_details.get("telemetry")
    telemetry: dict[str, Any] = telemetry_value if isinstance(telemetry_value, dict) else {}
    router_context = _router_context(task, source=source, context=context)
    logger.warning(
        "task router failed for task %s: %s; details=%s",
        task.id,
        error,
        error_details,
    )
    payload = _json_safe(
        redact(
            {
                "error": {
                    "code": "task_router_failed",
                    "message": str(error)[:500],
                    "details": error_details,
                },
                "context": router_context,
            }
        )
    )
    event = TaskEvent(task_id=task.id, kind="task.router_failed", payload=payload)
    db.add(event)
    await db.flush()
    model = str(
        error_details.get("model") or settings.task_router_model or settings.conversation_model
    )
    await record_llm_usage(
        db,
        component="task_router",
        model=model,
        status="error",
        input_tokens=int(error_details.get("input_units") or 0),
        cached_input_tokens=int(error_details.get("cached_input_tokens") or 0),
        output_tokens=int(error_details.get("output_units") or 0),
        reasoning_tokens=int(error_details.get("reasoning_tokens") or 0),
        task_id=task.id,
        reference_id=event.id,
        **telemetry,
    )
    await notify_router_failure(
        db,
        task_id=task.id,
        model=model,
        message=str(error),
        details=error_details,
    )
    await write_audit(
        db,
        actor=actor,
        source=source,
        action="task.router_decision",
        outcome="error",
        task_id=task.id,
        metadata=payload,
    )


def _normalize_decision(decision: TaskRouterDecision) -> TaskRouterDecision:
    data = decision.model_dump()
    if data["action"] not in TASK_ROUTER_ACTIONS:
        data["action"] = "operator_review"
    if data["category"] not in TASK_ROUTER_CATEGORIES:
        data["category"] = "unknown"
    if data["priority"] not in TASK_ROUTER_PRIORITIES:
        data["priority"] = "medium"
    if data["severity"] not in TASK_ROUTER_SEVERITIES:
        data["severity"] = "warning"
    if data["suggested_owner"] not in TASK_ROUTER_OWNERS:
        data["suggested_owner"] = "operator"
    data["labels"] = [_label(item) for item in data["labels"] if _label(item)][:8]
    data["first_steps"] = [str(item).strip()[:240] for item in data["first_steps"] if str(item).strip()][:5]
    return TaskRouterDecision.model_validate(data)


def _label(value: Any) -> str:
    return str(value).strip().lower().replace(" ", "-")[:40]


def _router_context(task: Task, *, source: str, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": {
            "id": task.id,
            "title": task.title,
            "goal": task.goal,
            "source": task.source,
            "status": task.status,
            "created_by": task.created_by,
            "assigned_agent": task.assigned_agent,
        },
        "source": source,
        "context": _json_safe(redact(context)),
        "allowed_actions": sorted(TASK_ROUTER_ACTIONS),
        "allowed_categories": sorted(TASK_ROUTER_CATEGORIES),
        "allowed_priorities": sorted(TASK_ROUTER_PRIORITIES),
        "allowed_severities": sorted(TASK_ROUTER_SEVERITIES),
        "allowed_owners": sorted(TASK_ROUTER_OWNERS),
    }


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _instructions() -> str:
    return (
        "You are Homelab Console Task Router. Return only structured JSON. "
        "Classify and route the task for human/agent review. Do not claim tasks, "
        "do not execute tools, do not propose write/remediation actions, and do "
        "not invent infrastructure facts. Suggest fixer only for a bounded, "
        "read-only investigation with a known runbook and no operator decision "
        "required. Prefer operator_review when uncertain."
    )


def _task_router_schema() -> dict[str, Any]:
    nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": sorted(TASK_ROUTER_ACTIONS)},
            "category": {"type": "string", "enum": sorted(TASK_ROUTER_CATEGORIES)},
            "priority": {"type": "string", "enum": sorted(TASK_ROUTER_PRIORITIES)},
            "severity": {"type": "string", "enum": sorted(TASK_ROUTER_SEVERITIES)},
            "suggested_owner": {"type": "string", "enum": sorted(TASK_ROUTER_OWNERS)},
            "runbook": nullable_string,
            "dedupe_candidate": nullable_string,
            "summary": {"type": "string"},
            "first_steps": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
            "labels": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
            "needs_operator": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "action",
            "category",
            "priority",
            "severity",
            "suggested_owner",
            "runbook",
            "dedupe_candidate",
            "summary",
            "first_steps",
            "labels",
            "needs_operator",
            "confidence",
        ],
    }


async def _post_openai_response(
    *,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> httpx.Response:
    last_error: TaskRouterError | None = None
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for attempt in range(3):
            try:
                response = await client.post(
                    "https://api.openai.com/v1/responses",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.TimeoutException as exc:
                last_error = TaskRouterError("task router model timed out")
                if attempt == 2:
                    raise last_error from exc
            except httpx.HTTPError as exc:
                last_error = TaskRouterError("task router model request failed")
                if attempt == 2:
                    raise last_error from exc
            else:
                if response.status_code not in {408, 409, 429, 500, 502, 503, 504}:
                    return response
                if attempt == 2:
                    return response
            await asyncio.sleep(0.4 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise TaskRouterError("task router model request failed")


def _extract_output_text(body: dict[str, Any]) -> str:
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    output = body.get("output")
    if not isinstance(output, list):
        return ""
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "".join(chunks)


def _response_diagnostics(
    body: dict[str, Any],
    *,
    output_text: str,
    http_status: int,
) -> dict[str, Any]:
    """Return response metadata that is safe to persist without model content."""
    diagnostics: dict[str, Any] = {
        "http_status": http_status,
        "output_text_length": len(output_text),
    }
    if body.get("model"):
        diagnostics["model"] = str(body["model"])[:64]
    response_status = body.get("status")
    if isinstance(response_status, str):
        diagnostics["response_status"] = response_status[:64]

    incomplete_details = body.get("incomplete_details")
    if isinstance(incomplete_details, dict):
        reason = incomplete_details.get("reason")
        if isinstance(reason, str):
            diagnostics["incomplete_reason"] = reason[:128]

    usage = body.get("usage")
    if isinstance(usage, dict):
        for source_key, target_key in (
            ("input_tokens", "input_units"),
            ("output_tokens", "output_units"),
        ):
            value = usage.get(source_key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                diagnostics[target_key] = value
        input_details = usage.get("input_tokens_details")
        if isinstance(input_details, dict):
            diagnostics["cached_input_tokens"] = int(input_details.get("cached_tokens") or 0)
        output_details = usage.get("output_tokens_details")
        if isinstance(output_details, dict):
            diagnostics["reasoning_tokens"] = int(output_details.get("reasoning_tokens") or 0)
    return diagnostics
