from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import LlmUsageEvent, TaskEvent, TaskRouterReview
from app.domain.actors import Actor
from app.services.luna_metrics import (
    _delivery_route_mode,
    backfill_llm_usage,
    luna_metrics,
    record_llm_usage,
    review_task_router,
)
from app.services.tasks_service import create_task
from tests.conftest import do_login


def test_delivery_route_does_not_reclassify_historical_schema():
    assert _delivery_route_mode("conversation-decision-v1") == "legacy"
    assert _delivery_route_mode("conversation-operations-question-v1") == "operations_question"


OPERATOR = Actor(kind="user", id="operator", label="operator")


async def test_luna_cost_uses_cached_input_rate(db_session):
    usage = await record_llm_usage(
        db_session,
        component="task_router",
        model="gpt-5.6-luna",
        status="success",
        input_tokens=1000,
        cached_input_tokens=100,
        output_tokens=100,
        reference_id="usage-1",
    )

    assert usage.attributed_cost_usd is not None
    assert usage.input_price_per_million is not None
    assert usage.cached_input_price_per_million is not None
    assert usage.output_price_per_million is not None
    assert Decimal(usage.attributed_cost_usd) == Decimal("0.00151000")
    assert Decimal(usage.input_price_per_million) == Decimal("1.000000")
    assert Decimal(usage.cached_input_price_per_million) == Decimal("0.100000")
    assert Decimal(usage.output_price_per_million) == Decimal("6.000000")
    assert usage.metered is True


async def test_luna_metrics_separates_reliability_from_reviewed_accuracy(db_session):
    accepted_task = await create_task(db_session, "Gateway warning", "Review routing", OPERATOR)
    rejected_task = await create_task(db_session, "UPS warning", "Review routing", OPERATOR)
    accepted_event = TaskEvent(
        task_id=accepted_task.id,
        kind="task.router_decision",
        payload={"decision": {"suggested_owner": "operator", "confidence": 0.9}},
    )
    rejected_event = TaskEvent(
        task_id=rejected_task.id,
        kind="task.router_decision",
        payload={"decision": {"suggested_owner": "fixer", "confidence": 0.7}},
    )
    db_session.add_all([accepted_event, rejected_event])
    await db_session.flush()
    await record_llm_usage(
        db_session,
        component="task_router",
        model="gpt-5.6-luna",
        status="success",
        input_tokens=100,
        output_tokens=10,
        task_id=accepted_task.id,
        reference_id=accepted_event.id,
    )
    await record_llm_usage(
        db_session,
        component="task_router",
        model="gpt-5.6-luna",
        status="success",
        input_tokens=100,
        output_tokens=10,
        task_id=rejected_task.id,
        reference_id=rejected_event.id,
    )
    await record_llm_usage(
        db_session,
        component="task_router",
        model="gpt-5.6-luna",
        status="error",
        task_id=accepted_task.id,
        reference_id="failed-call",
    )
    await review_task_router(
        db_session,
        task_id=accepted_task.id,
        verdict="accepted",
        corrections={},
        note="",
        actor=OPERATOR,
    )
    await review_task_router(
        db_session,
        task_id=rejected_task.id,
        verdict="rejected",
        corrections={},
        note="wrong owner",
        actor=OPERATOR,
    )

    result = await luna_metrics(db_session, days=30)

    assert result["router"]["successful_calls"] == 2
    assert result["router"]["failed_calls"] == 1
    assert result["router"]["technical_success_rate"] == 2 / 3
    assert result["router"]["reviewed"] == 2
    assert result["router"]["reviewed_accuracy"] == 0.5
    assert result["router"]["owner_distribution"] == {
        "fixer": 1,
        "operator": 1,
    }
    assert result["review_queue"] == []


async def test_luna_metrics_reports_ai_manager_telemetry(db_session):
    await record_llm_usage(
        db_session,
        component="conversation",
        model="ai-manager:Qwen3.5-4B-Q8_0",
        status="success",
        input_tokens=100,
        output_tokens=20,
        reference_id="local-call",
        provider="ai_manager",
        queue_wait_ms=10,
        inference_latency_ms=100,
        prompt_version="conversation-v1",
        schema_version="conversation-decision-v1",
        model_version="Qwen3.5-4B-Q8_0",
    )
    await record_llm_usage(
        db_session,
        component="task_router",
        model="gpt-5.6-luna",
        status="success",
        input_tokens=100,
        output_tokens=20,
        reference_id="fallback-call",
        provider="openai",
        fallback_used=True,
        fallback_reason="schema_error",
        error_kind="schema_error",
        queue_wait_ms=30,
        inference_latency_ms=300,
        prompt_version="task-router-v1",
        schema_version="task-router-decision-v1",
        model_version="Qwen3.5-4B-Q8_0",
    )

    result = await luna_metrics(db_session, days=30)

    assert result["ai_manager"]["calls"] == 2
    assert result["ai_manager"]["local_calls"] == 1
    assert result["ai_manager"]["fallback_calls"] == 1
    assert result["ai_manager"]["local_rate"] == 0.5
    assert result["ai_manager"]["fallback_rate"] == 0.5
    assert result["ai_manager"]["schema_errors"] == 1
    assert result["ai_manager"]["queue_wait"] == {"average_ms": 20.0, "p95_ms": 30, "samples": 2}
    assert result["ai_manager"]["inference_latency"] == {
        "average_ms": 200.0,
        "p95_ms": 300,
        "samples": 2,
    }


async def test_luna_metrics_reports_successful_and_failed_delivery_turns(db_session):
    await record_llm_usage(
        db_session,
        component="conversation",
        model="opencode-go/deepseek-v4-flash",
        status="success",
        reference_id="delivery-success",
        provider="opencode",
        inference_latency_ms=120,
        schema_version="conversation-chat-v1",
    )
    await record_llm_usage(
        db_session,
        component="conversation",
        model="openai",
        status="error",
        reference_id="delivery-failure",
        provider="openai",
        fallback_used=True,
        fallback_reason="opencode:timeout;ai_manager:transport",
        error_kind="transport",
        inference_latency_ms=480,
        schema_version="conversation-operations-shortcut-v1",
    )

    result = await luna_metrics(db_session, days=30)

    delivery = result["ai_delivery"]
    assert delivery["calls"] == 2
    assert delivery["successful_calls"] == 1
    assert delivery["failed_calls"] == 1
    assert delivery["fallback_calls"] == 1
    assert delivery["fallback_rate"] == 0.5
    assert delivery["latency"] == {"average_ms": 300.0, "p95_ms": 480, "samples": 2}
    assert delivery["providers"] == {"opencode": 1, "openai": 1}
    assert delivery["routes"] == {"chat": 1, "operations_shortcut": 1}
    assert [row["status"] for row in delivery["recent"]] == ["error", "success"]
    assert [row["route_mode"] for row in delivery["recent"]] == [
        "operations_shortcut",
        "chat",
    ]


async def test_luna_backfill_is_idempotent_and_marks_missing_usage(db_session):
    task = await create_task(db_session, "Router history", "Backfill telemetry", OPERATOR)
    event = TaskEvent(
        task_id=task.id,
        kind="task.router_decision",
        payload={
            "model": "gpt-5.6-luna",
            "input_tokens": 120,
            "output_tokens": 20,
            "decision": {"suggested_owner": "operator"},
        },
    )
    missing = TaskEvent(
        task_id=task.id,
        kind="task.router_failed",
        payload={"error": {"details": {"model": "gpt-5.6-luna"}}},
    )
    db_session.add_all([event, missing])
    await db_session.flush()

    assert await backfill_llm_usage(db_session) == 2
    assert await backfill_llm_usage(db_session) == 0

    count = await db_session.scalar(select(func.count()).select_from(LlmUsageEvent))
    assert count == 2
    failed = (
        await db_session.execute(
            select(LlmUsageEvent).where(LlmUsageEvent.reference_id == missing.id)
        )
    ).scalar_one()
    assert failed.status == "error"
    assert failed.metered is False


async def test_router_review_updates_single_canonical_row(db_session):
    task = await create_task(db_session, "Review me", "Review routing", OPERATOR)
    db_session.add(
        TaskEvent(
            task_id=task.id,
            kind="task.router_decision",
            payload={"decision": {"suggested_owner": "operator"}},
        )
    )
    await db_session.flush()

    first = await review_task_router(
        db_session,
        task_id=task.id,
        verdict="accepted",
        corrections={},
        note="",
        actor=OPERATOR,
    )
    second = await review_task_router(
        db_session,
        task_id=task.id,
        verdict="corrected",
        corrections={"suggested_owner": "fixer"},
        note="read-only investigation",
        actor=OPERATOR,
    )

    assert first.id == second.id
    assert second.verdict == "corrected"
    assert second.corrections == {"suggested_owner": "fixer"}
    reviews = await db_session.scalar(select(func.count()).select_from(TaskRouterReview))
    assert reviews == 1
    events = (
        await db_session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.kind == "task.router_reviewed")
        )
    ).scalars().all()
    assert len(events) == 2


async def test_luna_metrics_and_review_api(client, capture_adapter, db_session, user):
    task = await create_task(db_session, "API review", "Review from Luna window", OPERATOR)
    db_session.add(
        TaskEvent(
            task_id=task.id,
            kind="task.router_decision",
            payload={
                "model": "gpt-5.6-luna",
                "input_tokens": 100,
                "output_tokens": 10,
                "decision": {
                    "action": "operator_review",
                    "category": "network",
                    "priority": "high",
                    "severity": "warning",
                    "suggested_owner": "operator",
                    "needs_operator": True,
                    "confidence": 0.8,
                },
            },
        )
    )
    await db_session.commit()
    _, csrf = await do_login(client, capture_adapter)

    metrics = await client.get("/api/luna/metrics?days=30")
    assert metrics.status_code == 200, metrics.text
    assert metrics.json()["router"]["decisions"] == 1
    assert metrics.json()["review_queue"][0]["task_id"] == task.id
    assert metrics.json()["review_queue"][0] == {
        "task_id": task.id,
        "task_title": "API review",
        "created_at": metrics.json()["review_queue"][0]["created_at"],
        "action": "operator_review",
        "category": "network",
        "priority": "high",
        "severity": "warning",
        "suggested_owner": "operator",
        "needs_operator": True,
        "confidence": 0.8,
        "summary": "",
    }

    reviewed = await client.post(
        f"/api/luna/tasks/{task.id}/review",
        json={
            "verdict": "corrected",
            "corrections": {
                "action": "keep",
                "category": "dns",
                "priority": "medium",
                "severity": "info",
                "suggested_owner": "fixer",
                "needs_operator": False,
            },
            "note": "Routing verificato manualmente",
        },
        headers={"x-csrf-token": csrf},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["verdict"] == "corrected"
    assert reviewed.json()["note"] == "Routing verificato manualmente"
    assert reviewed.json()["corrections"]["needs_operator"] is False

    refreshed = await client.get("/api/luna/metrics?days=30")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["router"]["corrected"] == 1
    assert refreshed.json()["review_queue"] == []
