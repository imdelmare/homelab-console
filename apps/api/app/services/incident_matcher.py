"""Hybrid matching of new watcher incidents against recently resolved work."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import AuditEvent, Incident, Task
from app.services.ai_manager import (
    AIManagerError,
    ai_manager_available,
    mark_ai_manager_available,
    mark_ai_manager_unavailable,
    request_ai_manager,
)
from app.services.opencode_go import OpenCodeGoError, request_structured_decision
from app.services.task_router import TaskRouterError, _extract_output_text, _post_openai_response

INCIDENT_MATCHER_PROMPT_VERSION = "incident-matcher-v1"
INCIDENT_MATCHER_SCHEMA_VERSION = "incident-match-v1"


class IncidentMatchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: str = Field(pattern="^(new|possible_match|already_handled)$")
    matched_incident_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=500)
    method: str = Field(pattern="^(deterministic|llm|fallback)$")
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    telemetry: dict[str, Any] = Field(default_factory=dict)


class IncidentMatchModel(Protocol):
    async def decide(self, context: dict[str, Any]) -> IncidentMatchDecision: ...


class OpenAIIncidentMatchModel:
    async def decide(self, context: dict[str, Any]) -> IncidentMatchDecision:
        settings = get_settings()
        model = settings.task_router_model or settings.conversation_model
        if not settings.openai_api_key:
            raise TaskRouterError("incident matcher model is not configured")
        payload = {
            "model": model,
            "reasoning": {"effort": settings.task_router_reasoning_effort},
            "instructions": (
                "Determine whether the new homelab incident describes work already handled by one "
                "of the supplied resolved candidates. Use only supplied facts. Return already_handled "
                "only for the same underlying fault and resource; possible_match when plausible but "
                "uncertain; otherwise new. Return only structured JSON."
            ),
            "input": [{"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)}],
            "text": {"format": {"type": "json_schema", "name": "incident_match", "strict": True, "schema": _schema()}},
            "max_output_tokens": min(settings.task_router_max_output_tokens, 300),
            "store": False,
        }
        response = await _post_openai_response(
            api_key=settings.openai_api_key,
            payload=payload,
            timeout_seconds=settings.task_router_timeout_seconds,
        )
        if response.status_code >= 400:
            raise TaskRouterError("incident matcher model request failed")
        body = response.json()
        try:
            raw = json.loads(_extract_output_text(body))
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            return IncidentMatchDecision.model_validate(
                {
                    **raw,
                    "method": "llm",
                    "model": model,
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                }
            )
        except (ValueError, ValidationError) as exc:
            raise TaskRouterError("incident matcher returned invalid structured output") from exc


class AIManagerIncidentMatchModel:
    async def decide(self, context: dict[str, Any]) -> IncidentMatchDecision:
        settings = get_settings()
        try:
            result = await request_ai_manager(
                instructions=_instructions(),
                context=context,
                schema_name="incident_match",
                schema=_schema(),
                max_output_tokens=min(settings.task_router_max_output_tokens, 300),
                timeout_seconds=settings.task_router_timeout_seconds,
                prompt_version=INCIDENT_MATCHER_PROMPT_VERSION,
                schema_version=INCIDENT_MATCHER_SCHEMA_VERSION,
            )
        except AIManagerError as exc:
            raise TaskRouterError(str(exc), details={"telemetry": exc.telemetry}) from exc
        try:
            raw = json.loads(result.output_text)
            return IncidentMatchDecision.model_validate(
                {
                    **raw,
                    "method": "llm",
                    "model": result.model,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "telemetry": result.telemetry,
                }
            )
        except (ValueError, ValidationError) as exc:
            raise TaskRouterError(
                "incident matcher returned invalid structured output",
                details={"telemetry": {**result.telemetry, "error_kind": "schema_error"}},
            ) from exc


class OpenCodeGoIncidentMatchModel:
    async def decide(self, context: dict[str, Any]) -> IncidentMatchDecision:
        settings = get_settings()
        model = settings.opencode_go_router_model
        try:
            result = await request_structured_decision(
                model=model,
                instructions=_instructions(),
                context=context,
                schema_name="incident_match",
                schema=_schema(),
                max_output_tokens=min(settings.task_router_max_output_tokens, 300),
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
                        "prompt_version": INCIDENT_MATCHER_PROMPT_VERSION,
                        "schema_version": INCIDENT_MATCHER_SCHEMA_VERSION,
                        "model_version": f"opencode-go/{model}",
                    },
                },
            ) from exc
        try:
            raw = json.loads(result.output_text)
            return IncidentMatchDecision.model_validate(
                {
                    **raw,
                    "method": "llm",
                    "model": f"opencode-go/{result.model}",
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "telemetry": {
                        "provider": "opencode_go",
                        "prompt_version": INCIDENT_MATCHER_PROMPT_VERSION,
                        "schema_version": INCIDENT_MATCHER_SCHEMA_VERSION,
                        "model_version": f"opencode-go/{result.model}",
                    },
                }
            )
        except (ValueError, ValidationError) as exc:
            raise TaskRouterError(
                "incident matcher returned invalid structured output",
                details={
                    "model": f"opencode-go/{result.model}",
                    "input_units": result.input_tokens,
                    "cached_input_tokens": result.cached_input_tokens,
                    "output_units": result.output_tokens,
                    "reasoning_tokens": result.reasoning_tokens,
                    "telemetry": {
                        "provider": "opencode_go",
                        "error_kind": "schema_error",
                        "prompt_version": INCIDENT_MATCHER_PROMPT_VERSION,
                        "schema_version": INCIDENT_MATCHER_SCHEMA_VERSION,
                        "model_version": f"opencode-go/{result.model}",
                    },
                },
            ) from exc


class AIManagerWithOpenAIFallbackIncidentMatchModel:
    def __init__(
        self,
        primary: IncidentMatchModel | None = None,
        fallback: IncidentMatchModel | None = None,
    ) -> None:
        self.primary = primary or AIManagerIncidentMatchModel()
        self.fallback = fallback or OpenAIIncidentMatchModel()

    async def decide(self, context: dict[str, Any]) -> IncidentMatchDecision:
        primary_telemetry: dict[str, Any]
        if ai_manager_available():
            try:
                result = await self.primary.decide(context)
            except TaskRouterError as exc:
                mark_ai_manager_unavailable()
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
                "prompt_version": INCIDENT_MATCHER_PROMPT_VERSION,
                "schema_version": INCIDENT_MATCHER_SCHEMA_VERSION,
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


def get_incident_match_model() -> IncidentMatchModel:
    settings = get_settings()
    provider = settings.task_router_provider.strip().lower() or settings.conversation_provider.strip().lower()
    if provider == "ai_manager":
        return AIManagerWithOpenAIFallbackIncidentMatchModel()
    if provider == "opencode_go":
        return OpenCodeGoIncidentMatchModel()
    return OpenAIIncidentMatchModel()


async def match_incident(
    db: AsyncSession,
    detected: Any,
    *,
    now: datetime,
    model: IncidentMatchModel | None = None,
) -> IncidentMatchDecision:
    settings = get_settings()
    if not getattr(settings, "incident_matcher_enabled", True):
        return _new("matcher disabled")

    max_candidates = getattr(settings, "incident_matcher_max_candidates", 5)
    candidates = await _candidates(db, detected, now=now, limit=max_candidates)
    if not candidates:
        return _new("no relevant resolved incidents")
    scored = sorted(
        [(_score(detected, incident, now), incident, task) for incident, task in candidates],
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best_incident, _ = scored[0]
    if best_score >= 95:
        outcome = "possible_match" if detected.severity == "critical" else "already_handled"
        return IncidentMatchDecision(
            outcome=outcome,
            matched_incident_id=best_incident.id,
            confidence=min(best_score / 100, 1.0),
            reason="high-confidence deterministic match",
            method="deterministic",
        )
    if best_score < 40:
        return _new("candidate score below ambiguity threshold")

    calls = await db.scalar(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.action == "watcher.incident_matcher",
            AuditEvent.created_at >= now - timedelta(hours=1),
        )
    )
    if int(calls or 0) >= getattr(settings, "incident_matcher_max_calls_per_hour", 10):
        return IncidentMatchDecision(
            outcome="possible_match",
            matched_incident_id=best_incident.id,
            confidence=best_score / 100,
            reason="LLM hourly limit reached",
            method="fallback",
        )

    context = {
        "new_incident": _incident_data(detected),
        "candidates": [
            {**_candidate_data(incident, task), "deterministic_score": score}
            for score, incident, task in scored[:max_candidates]
        ],
    }
    try:
        decision = await (model or get_incident_match_model()).decide(context)
    except Exception as exc:
        details = exc.details if isinstance(exc, TaskRouterError) else {}
        telemetry_value = details.get("telemetry")
        telemetry = telemetry_value if isinstance(telemetry_value, dict) else {}
        return IncidentMatchDecision(
            outcome="possible_match",
            matched_incident_id=best_incident.id,
            confidence=best_score / 100,
            reason=f"matcher unavailable: {type(exc).__name__}",
            method="fallback",
            model=str(details.get("model") or ""),
            input_tokens=int(details.get("input_units") or 0),
            output_tokens=int(details.get("output_units") or 0),
            telemetry=telemetry,
        )
    candidate_ids = {incident.id for _, incident, _ in scored}
    if decision.matched_incident_id not in candidate_ids:
        decision.matched_incident_id = None
        decision.outcome = "new"
        decision.reason = "model selected no valid candidate"
    if detected.severity == "critical" and decision.outcome == "already_handled":
        decision.outcome = "possible_match"
        decision.reason = f"critical incident requires operator review: {decision.reason}"
    if decision.outcome == "already_handled" and decision.confidence < getattr(
        settings, "incident_matcher_auto_handle_confidence", 0.9
    ):
        decision.outcome = "possible_match"
    return decision


async def _candidates(db: AsyncSession, detected: Any, *, now: datetime, limit: int):
    rows = await db.execute(
        select(Incident, Task)
        .join(Task, Task.id == Incident.task_id)
        .where(
            Incident.status == "resolved",
            Incident.resolved_at >= now - timedelta(days=7),
            or_(Incident.provider_id == detected.provider_id, Incident.watcher_id == detected.watcher_id),
        )
        .order_by(Incident.resolved_at.desc())
        .limit(max(1, min(limit, 10)))
    )
    return rows.all()


def _score(detected: Any, incident: Incident, now: datetime) -> int:
    if detected.dedupe_key == incident.dedupe_key:
        return 100
    score = 0
    if detected.provider_id == incident.provider_id:
        score += 30
    if detected.watcher_id == incident.watcher_id:
        score += 20
    overlap = _token_overlap(f"{detected.title} {detected.description}", f"{incident.title} {incident.description}")
    score += 20 if overlap >= 0.5 else 10 if overlap >= 0.25 else 0
    age = now - (incident.resolved_at or incident.last_seen_at)
    score += 20 if age <= timedelta(hours=2) else 10 if age <= timedelta(days=1) else 0
    return score


def _token_overlap(left: str, right: str) -> float:
    ignored = {"the", "and", "is", "are", "di", "il", "la", "un", "una", "in", "su"}
    a = {item for item in re.findall(r"[a-z0-9_.-]+", left.lower()) if len(item) > 2 and item not in ignored}
    b = {item for item in re.findall(r"[a-z0-9_.-]+", right.lower()) if len(item) > 2 and item not in ignored}
    return len(a & b) / max(1, min(len(a), len(b)))


def _incident_data(detected: Any) -> dict[str, Any]:
    return {key: getattr(detected, key) for key in ("watcher_id", "dedupe_key", "severity", "provider_id", "title", "description")}


def _candidate_data(incident: Incident, task: Task) -> dict[str, Any]:
    return {"incident_id": incident.id, "provider_id": incident.provider_id, "watcher_id": incident.watcher_id, "title": incident.title, "description": incident.description[:800], "resolved_at": incident.resolved_at, "resolution_reason": incident.resolution_reason, "task_title": task.title, "task_summary": task.summary[:800]}


def _new(reason: str) -> IncidentMatchDecision:
    return IncidentMatchDecision(outcome="new", confidence=1.0, reason=reason, method="deterministic")


def _instructions() -> str:
    return (
        "Determine whether the new homelab incident describes work already handled by one "
        "of the supplied resolved candidates. Use only supplied facts. Return already_handled "
        "only for the same underlying fault and resource; possible_match when plausible but "
        "uncertain; otherwise new. Return only structured JSON."
    )


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "outcome": {"type": "string", "enum": ["new", "possible_match", "already_handled"]},
            "matched_incident_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
        "required": ["outcome", "matched_incident_id", "confidence", "reason"],
    }
