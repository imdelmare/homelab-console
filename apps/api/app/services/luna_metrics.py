"""Canonical Luna usage, cost attribution, and Task Router review metrics."""

from __future__ import annotations

from collections.abc import Sequence
from collections import Counter
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ConversationMessage,
    LlmUsageEvent,
    Task,
    TaskEvent,
    TaskRouterReview,
    utcnow,
)
from app.domain.actors import Actor
from app.services.audit import write_audit
from app.services.tasks_service import TaskServiceError

LUNA_PRICING_SOURCE = "openai-standard-2026-07-09"
MODEL_PRICING_USD_PER_MILLION: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "gpt-5.6-luna": (Decimal("1.00"), Decimal("0.10"), Decimal("6.00")),
}
COMPONENT_LABELS = {
    "task_router": "Task Router",
    "incident_matcher": "Incident Matcher",
    "conversation": "Conversation",
}
ROUTER_VERDICTS = {"accepted", "corrected", "rejected"}


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _cost(
    model: str,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> tuple[Decimal | None, tuple[Decimal, Decimal, Decimal] | None]:
    prices = MODEL_PRICING_USD_PER_MILLION.get(model)
    if prices is None:
        return None, None
    input_price, cached_price, output_price = prices
    uncached = max(0, input_tokens - cached_input_tokens)
    value = (
        Decimal(uncached) * input_price
        + Decimal(cached_input_tokens) * cached_price
        + Decimal(output_tokens) * output_price
    ) / Decimal(1_000_000)
    return value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP), prices


async def record_llm_usage(
    db: AsyncSession,
    *,
    component: str,
    model: str,
    status: str,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    task_id: str | None = None,
    reference_id: str | None = None,
    provider: str = "",
    fallback_used: bool = False,
    fallback_reason: str = "",
    error_kind: str = "",
    queue_wait_ms: int | None = None,
    inference_latency_ms: int | None = None,
    prompt_version: str = "",
    schema_version: str = "",
    model_version: str = "",
    created_at=None,
) -> LlmUsageEvent:
    if reference_id:
        existing = (
            await db.execute(
                select(LlmUsageEvent).where(
                    LlmUsageEvent.component == component,
                    LlmUsageEvent.reference_id == reference_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    input_tokens = _integer(input_tokens)
    cached_input_tokens = min(input_tokens, _integer(cached_input_tokens))
    output_tokens = _integer(output_tokens)
    reasoning_tokens = min(output_tokens, _integer(reasoning_tokens))
    metered = input_tokens > 0 or output_tokens > 0
    attributed_cost, prices = _cost(
        model,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
    )
    if not metered:
        attributed_cost = None
    row = LlmUsageEvent(
        component=component,
        reference_id=reference_id,
        task_id=task_id,
        model=model[:64],
        status="success" if status == "success" else "error",
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        metered=metered,
        input_price_per_million=prices[0] if prices else None,
        cached_input_price_per_million=prices[1] if prices else None,
        output_price_per_million=prices[2] if prices else None,
        attributed_cost_usd=attributed_cost,
        pricing_source=LUNA_PRICING_SOURCE if prices else "",
        provider=provider[:32],
        fallback_used=bool(fallback_used),
        fallback_reason=fallback_reason[:64],
        error_kind=error_kind[:32],
        queue_wait_ms=_integer(queue_wait_ms) if queue_wait_ms is not None else None,
        inference_latency_ms=(
            _integer(inference_latency_ms) if inference_latency_ms is not None else None
        ),
        prompt_version=prompt_version[:64],
        schema_version=schema_version[:64],
        model_version=model_version[:128],
        created_at=created_at or utcnow(),
    )
    db.add(row)
    await db.flush()
    return row


def _usage_from_payload(payload: dict[str, Any], *, nested: bool = False) -> dict[str, int]:
    source = payload.get("decision") if nested and isinstance(payload.get("decision"), dict) else payload
    if not isinstance(source, dict):
        source = {}
    details = _mapping(source.get("input_tokens_details"))
    output_details = _mapping(source.get("output_tokens_details"))
    return {
        "input_tokens": _integer(source.get("input_tokens") or source.get("input_units")),
        "cached_input_tokens": _integer(details.get("cached_tokens")),
        "output_tokens": _integer(source.get("output_tokens") or source.get("output_units")),
        "reasoning_tokens": _integer(output_details.get("reasoning_tokens")),
    }


async def backfill_llm_usage(db: AsyncSession) -> int:
    """Idempotently recover metering already present in canonical history."""

    created = 0
    events = (
        await db.execute(
            select(TaskEvent).where(
                TaskEvent.kind.in_(
                    (
                        "task.router_decision",
                        "task.router_failed",
                        "watcher.incident.auto_matched",
                        "watcher.incident.possible_match",
                    )
                )
            )
        )
    ).scalars().all()
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.kind == "task.router_decision":
            model = str(payload.get("model") or "")
            usage = _usage_from_payload(payload)
            component = "task_router"
            status = "success"
        elif event.kind == "task.router_failed":
            error = _mapping(payload.get("error"))
            details = _mapping(error.get("details"))
            model = str(details.get("model") or "gpt-5.6-luna")
            usage = _usage_from_payload(details)
            component = "task_router"
            status = "error"
        else:
            decision = _mapping(payload.get("decision"))
            model = str(decision.get("model") or "")
            if not model:
                continue
            usage = _usage_from_payload(payload, nested=True)
            component = "incident_matcher"
            status = "success"
        before = await db.scalar(
            select(LlmUsageEvent.id).where(
                LlmUsageEvent.component == component,
                LlmUsageEvent.reference_id == event.id,
            )
        )
        if before is not None:
            continue
        await record_llm_usage(
            db,
            component=component,
            model=model,
            status=status,
            task_id=event.task_id,
            reference_id=event.id,
            created_at=event.created_at,
            input_tokens=usage["input_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            output_tokens=usage["output_tokens"],
            reasoning_tokens=usage["reasoning_tokens"],
        )
        created += 1

    messages = (
        await db.execute(
            select(ConversationMessage).where(
                ConversationMessage.role == "assistant",
                ConversationMessage.model != "",
            )
        )
    ).scalars().all()
    for message in messages:
        before = await db.scalar(
            select(LlmUsageEvent.id).where(
                LlmUsageEvent.component == "conversation",
                LlmUsageEvent.reference_id == message.id,
            )
        )
        if before is not None:
            continue
        await record_llm_usage(
            db,
            component="conversation",
            model=message.model,
            status="success",
            input_tokens=message.input_tokens,
            output_tokens=message.output_tokens,
            reference_id=message.id,
            created_at=message.created_at,
        )
        created += 1
    return created


def _usage_summary(rows: Sequence[LlmUsageEvent]) -> dict[str, Any]:
    total = len(rows)
    metered = sum(1 for row in rows if row.metered)
    priced = sum(1 for row in rows if row.metered and row.attributed_cost_usd is not None)
    return {
        "calls": total,
        "successful_calls": sum(1 for row in rows if row.status == "success"),
        "failed_calls": sum(1 for row in rows if row.status != "success"),
        "metered_calls": metered,
        "metering_coverage": metered / total if total else 0.0,
        "priced_calls": priced,
        "pricing_coverage": priced / metered if metered else 0.0,
        "input_tokens": sum(row.input_tokens for row in rows),
        "cached_input_tokens": sum(row.cached_input_tokens for row in rows),
        "output_tokens": sum(row.output_tokens for row in rows),
        "reasoning_tokens": sum(row.reasoning_tokens for row in rows),
        "attributed_cost_usd": float(
            sum((Decimal(row.attributed_cost_usd) for row in rows if row.attributed_cost_usd is not None), Decimal(0))
        ),
    }


def _latency_summary(values: list[int]) -> dict[str, float | int | None]:
    if not values:
        return {"average_ms": None, "p95_ms": None, "samples": 0}
    ordered = sorted(values)
    p95_index = max(0, (95 * len(ordered) + 99) // 100 - 1)
    return {
        "average_ms": sum(ordered) / len(ordered),
        "p95_ms": ordered[p95_index],
        "samples": len(ordered),
    }


def _delivery_route_mode(schema_version: str) -> str:
    if schema_version == "conversation-chat-v1":
        return "chat"
    if schema_version == "conversation-operations-shortcut-v1":
        return "operations_shortcut"
    if schema_version == "conversation-operations-question-v1":
        return "operations_question"
    return "legacy"


async def luna_metrics(db: AsyncSession, *, days: int = 30) -> dict[str, Any]:
    days = max(1, min(days, 365))
    since = utcnow() - timedelta(days=days)
    rows = (
        await db.execute(
            select(LlmUsageEvent)
            .where(LlmUsageEvent.created_at >= since)
            .order_by(LlmUsageEvent.created_at.desc())
        )
    ).scalars().all()
    components = []
    for component in COMPONENT_LABELS:
        component_rows = [row for row in rows if row.component == component]
        components.append(
            {
                "component": component,
                "label": COMPONENT_LABELS[component],
                **_usage_summary(component_rows),
            }
        )

    router_rows = [row for row in rows if row.component == "task_router"]
    decision_events = (
        await db.execute(
            select(TaskEvent).where(
                TaskEvent.kind == "task.router_decision",
                TaskEvent.created_at >= since,
            )
        )
    ).scalars().all()
    router_task_ids = {event.task_id for event in decision_events}
    reviews = []
    if router_task_ids:
        reviews = (
            await db.execute(
                select(TaskRouterReview).where(TaskRouterReview.task_id.in_(router_task_ids))
            )
        ).scalars().all()
    review_counts = Counter(review.verdict for review in reviews)
    reviewed = len(reviews)
    owners = Counter()
    confidences: list[float] = []
    for event in decision_events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        decision = _mapping(payload.get("decision"))
        owners[str(decision.get("suggested_owner") or "unknown")] += 1
        confidence = decision.get("confidence")
        if confidence is None:
            continue
        try:
            confidences.append(float(confidence))
        except (TypeError, ValueError):
            pass

    auto_events = (
        await db.execute(
            select(TaskEvent).where(
                TaskEvent.kind == "watcher.auto_investigate",
                TaskEvent.created_at >= since,
            )
        )
    ).scalars().all()
    auto_outcomes = Counter(
        str(event.payload.get("outcome") or "unknown")
        for event in auto_events
        if isinstance(event.payload, dict)
    )

    reviewed_event_ids = select(TaskRouterReview.decision_event_id)
    queue_rows = (
        await db.execute(
            select(TaskEvent, Task)
            .join(Task, Task.id == TaskEvent.task_id)
            .where(
                TaskEvent.kind == "task.router_decision",
                TaskEvent.created_at >= since,
                TaskEvent.id.not_in(reviewed_event_ids),
            )
            .order_by(TaskEvent.created_at.desc())
            .limit(12)
        )
    ).all()
    review_queue = []
    for event, task in queue_rows:
        payload = event.payload if isinstance(event.payload, dict) else {}
        decision = _mapping(payload.get("decision"))
        review_queue.append(
            {
                "task_id": task.id,
                "task_title": task.title,
                "created_at": event.created_at,
                "action": str(decision.get("action") or "operator_review"),
                "category": str(decision.get("category") or "unknown"),
                "priority": str(decision.get("priority") or "medium"),
                "severity": str(decision.get("severity") or "warning"),
                "suggested_owner": str(decision.get("suggested_owner") or "unknown"),
                "needs_operator": bool(decision.get("needs_operator")),
                "confidence": decision.get("confidence"),
                "summary": str(decision.get("summary") or ""),
            }
        )

    router_total = len(router_rows)
    accepted = review_counts["accepted"]
    ai_rows = [
        row
        for row in rows
        if row.provider in {"ai_manager", "openai"}
        and (row.prompt_version or row.fallback_used or row.provider == "ai_manager")
    ]
    fallback_calls = sum(1 for row in ai_rows if row.fallback_used)
    local_calls = len(ai_rows) - fallback_calls
    queue_values = [row.queue_wait_ms for row in ai_rows if row.queue_wait_ms is not None]
    inference_values = [
        row.inference_latency_ms for row in ai_rows if row.inference_latency_ms is not None
    ]
    effective_models = Counter(row.model or "unknown" for row in ai_rows)
    prompt_versions = Counter(row.prompt_version or "unknown" for row in ai_rows)
    schema_versions = Counter(row.schema_version or "unknown" for row in ai_rows)
    model_versions = Counter(row.model_version or "unknown" for row in ai_rows)
    conversation_rows = [row for row in rows if row.component == "conversation"]
    conversation_latencies = [
        row.inference_latency_ms
        for row in conversation_rows
        if row.inference_latency_ms is not None
    ]
    conversation_providers = Counter(row.provider or "unknown" for row in conversation_rows)
    conversation_models = Counter(row.model or "unknown" for row in conversation_rows)
    conversation_routes = Counter(
        _delivery_route_mode(row.schema_version) for row in conversation_rows
    )
    conversation_fallbacks = sum(1 for row in conversation_rows if row.fallback_used)
    return {
        "period_days": days,
        "since": since,
        "pricing": {
            "model": "gpt-5.6-luna",
            "input_per_million": 1.0,
            "cached_input_per_million": 0.1,
            "output_per_million": 6.0,
            "source": LUNA_PRICING_SOURCE,
            "billing_reconciled": False,
        },
        "summary": _usage_summary(rows),
        "components": components,
        "ai_manager": {
            "calls": len(ai_rows),
            "local_calls": local_calls,
            "fallback_calls": fallback_calls,
            "local_rate": local_calls / len(ai_rows) if ai_rows else 0.0,
            "fallback_rate": fallback_calls / len(ai_rows) if ai_rows else 0.0,
            "schema_errors": sum(1 for row in ai_rows if row.error_kind == "schema_error"),
            "timeouts": sum(1 for row in ai_rows if row.error_kind == "timeout"),
            "queue_wait": _latency_summary(queue_values),
            "inference_latency": _latency_summary(inference_values),
            "effective_models": dict(sorted(effective_models.items())),
            "prompt_versions": dict(sorted(prompt_versions.items())),
            "schema_versions": dict(sorted(schema_versions.items())),
            "model_versions": dict(sorted(model_versions.items())),
        },
        "ai_delivery": {
            "calls": len(conversation_rows),
            "successful_calls": sum(1 for row in conversation_rows if row.status == "success"),
            "failed_calls": sum(1 for row in conversation_rows if row.status != "success"),
            "fallback_calls": conversation_fallbacks,
            "fallback_rate": (
                conversation_fallbacks / len(conversation_rows) if conversation_rows else 0.0
            ),
            "latency": _latency_summary(conversation_latencies),
            "providers": dict(sorted(conversation_providers.items())),
            "models": dict(sorted(conversation_models.items())),
            "routes": dict(sorted(conversation_routes.items())),
            "recent": [
                {
                    "id": row.id,
                    "created_at": row.created_at,
                    "provider": row.provider or "unknown",
                    "model": row.model or "unknown",
                    "status": row.status,
                    "fallback_used": row.fallback_used,
                    "fallback_reason": row.fallback_reason,
                    "error_kind": row.error_kind,
                    "route_mode": _delivery_route_mode(row.schema_version),
                    "inference_latency_ms": row.inference_latency_ms,
                }
                for row in conversation_rows[:20]
            ],
        },
        "router": {
            "decisions": len(decision_events),
            "successful_calls": sum(1 for row in router_rows if row.status == "success"),
            "failed_calls": sum(1 for row in router_rows if row.status != "success"),
            "technical_success_rate": (
                sum(1 for row in router_rows if row.status == "success") / router_total
                if router_total
                else 0.0
            ),
            "reviewed": reviewed,
            "review_coverage": reviewed / len(decision_events) if decision_events else 0.0,
            "accepted": accepted,
            "corrected": review_counts["corrected"],
            "rejected": review_counts["rejected"],
            "reviewed_accuracy": accepted / reviewed if reviewed else None,
            "average_confidence": sum(confidences) / len(confidences) if confidences else None,
            "owner_distribution": dict(sorted(owners.items())),
        },
        "auto_investigate": dict(sorted(auto_outcomes.items())),
        "review_queue": review_queue,
    }


async def review_task_router(
    db: AsyncSession,
    *,
    task_id: str,
    verdict: str,
    corrections: dict[str, Any],
    note: str,
    actor: Actor,
) -> TaskRouterReview:
    if verdict not in ROUTER_VERDICTS:
        raise TaskServiceError("invalid_input", "invalid router review verdict")
    task = await db.get(Task, task_id)
    if task is None:
        raise TaskServiceError("unknown_task", "unknown task")
    decision_event = (
        await db.execute(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.kind == "task.router_decision")
            .order_by(TaskEvent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if decision_event is None:
        raise TaskServiceError("invalid_input", "task has no router decision")
    review = (
        await db.execute(select(TaskRouterReview).where(TaskRouterReview.task_id == task_id))
    ).scalar_one_or_none()
    if review is None:
        review = TaskRouterReview(
            task_id=task_id,
            decision_event_id=decision_event.id,
            verdict=verdict,
            corrections=corrections,
            note=note[:1000],
            reviewed_by=actor.audit_id(),
        )
        db.add(review)
    else:
        review.verdict = verdict
        review.corrections = corrections
        review.note = note[:1000]
        review.reviewed_by = actor.audit_id()
        review.updated_at = utcnow()
    payload = {
        "verdict": verdict,
        "corrections": corrections,
        "note": note[:1000],
        "decision_event_id": decision_event.id,
    }
    db.add(TaskEvent(task_id=task_id, kind="task.router_reviewed", payload=payload))
    await write_audit(
        db,
        actor=actor,
        source="rest",
        action="task.router_reviewed",
        outcome=verdict,
        task_id=task_id,
        metadata=payload,
    )
    await db.flush()
    return review
