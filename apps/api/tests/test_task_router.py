import httpx
import pytest
from sqlalchemy import select

from app.core.settings import get_settings
from app.db.models import AuditEvent, NotificationOutbox, TaskEvent
from app.domain.actors import Actor
from app.services.opencode_go import OpenCodeGoError, OpenCodeGoResult
from app.services.task_router import (
    AIManagerWithOpenAIFallbackTaskRouterModel,
    OpenCodeTaskRouterModel,
    OpenAITaskRouterModel,
    TaskRouterDecision,
    TaskRouterError,
    TaskRouterResult,
    get_task_router_model,
    route_task,
)
from app.services.tasks_service import create_task

OPERATOR = Actor(kind="user", id="operator", label="operator")


class FakeRouterModel:
    async def decide(self, context):
        assert context["task"]["title"] == "Gateway down"
        assert context["context"]["provider_id"] == "opnsense"
        return TaskRouterResult(
            model="fake-luna",
            input_tokens=11,
            output_tokens=7,
            decision=TaskRouterDecision(
                action="keep",
                category="network",
                priority="high",
                severity="critical",
                suggested_owner="claude",
                runbook="gateway_alert",
                dedupe_candidate=None,
                summary="Gateway incident should be investigated with OPNsense read-only checks.",
                first_steps=["Run opnsense.summary", "Run opnsense.gateways.status"],
                labels=["Network", "Gateway Alert"],
                needs_operator=False,
                confidence=0.82,
            ),
        )


class InvalidRouterModel:
    async def decide(self, context: dict[str, object]) -> TaskRouterResult:
        return TaskRouterResult(
            model="fake-luna",
            decision=TaskRouterDecision(
                action="bad-action",
                category="not-a-category",
                priority="urgent-ish",
                severity="page-me",
                suggested_owner="nobody",
                runbook=None,
                dedupe_candidate=None,
                summary="fallback",
                first_steps=["x"],
                labels=["Needs Operator"],
                needs_operator=True,
                confidence=1.0,
            ),
        )


class FailingRouterModel:
    async def decide(self, context: dict[str, object]) -> TaskRouterResult:
        raise TaskRouterError(
            "task router model response incomplete",
            details={
                "http_status": 200,
                "response_status": "incomplete",
                "incomplete_reason": "max_output_tokens",
                "output_text_length": 417,
                "output_units": 300,
            },
        )


def test_task_router_inherits_ai_manager_provider(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_provider", "ai_manager")
    monkeypatch.setattr(settings, "task_router_provider", "")

    assert isinstance(get_task_router_model(), AIManagerWithOpenAIFallbackTaskRouterModel)


async def test_openai_router_reports_incomplete_response_without_model_content(monkeypatch):
    async def fake_post_openai_response(**_kwargs):
        return httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"content": [{"type": "output_text", "text": "sensitive partial output"}]}],
                "usage": {"input_tokens": 123, "output_tokens": 500},
            },
        )

    monkeypatch.setattr(
        "app.services.task_router._post_openai_response",
        fake_post_openai_response,
    )
    model = OpenAITaskRouterModel()
    model.api_key = "test-key"

    with pytest.raises(TaskRouterError, match="response incomplete") as exc_info:
        await model.decide({"task": {"title": "test"}})

    assert exc_info.value.details == {
        "http_status": 200,
        "output_text_length": 24,
        "response_status": "incomplete",
        "incomplete_reason": "max_output_tokens",
        "input_units": 123,
        "output_units": 500,
    }
    assert "sensitive" not in str(exc_info.value)


async def test_opencode_router_uses_direct_go_contract(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "opencode_go_api_key", "go-key")
    captured = {}

    async def fake_request(**kwargs):
        captured.update(kwargs)
        return OpenCodeGoResult(
            output_text=(
                '{"action":"operator_review","category":"network","priority":"medium",'
                '"severity":"warning","suggested_owner":"operator","runbook":null,'
                '"dedupe_candidate":null,"summary":"Operator review requested.",'
                '"first_steps":[],"labels":["Network"],"needs_operator":true,'
                '"confidence":0.7}'
            ),
            model="deepseek-v4-pro",
            input_tokens=12,
            output_tokens=8,
        )

    monkeypatch.setattr("app.services.task_router.request_structured_decision", fake_request)

    result = await OpenCodeTaskRouterModel().decide({"task": {"title": "Gateway"}})

    assert result.model == "opencode-go/deepseek-v4-pro"
    assert result.decision.labels == ["network"]
    assert captured["model"] == "deepseek-v4-pro"
    assert captured["schema"]["additionalProperties"] is False
    assert result.telemetry["provider"] == "opencode_go"


async def test_opencode_router_preserves_transient_failure(monkeypatch):
    monkeypatch.setattr(get_settings(), "opencode_go_api_key", "go-key")

    async def fake_request(**_kwargs):
        raise OpenCodeGoError("provider busy", error_kind="http_error", transient=True, http_status=503)

    monkeypatch.setattr("app.services.task_router.request_structured_decision", fake_request)

    with pytest.raises(TaskRouterError, match="provider busy") as exc_info:
        await OpenCodeTaskRouterModel().decide({"task": {"title": "Gateway"}})

    assert exc_info.value.details["transient"] is True
    assert exc_info.value.details["http_status"] == 503


async def test_route_task_records_router_decision_event_and_audit(db_session):
    task = await create_task(db_session, "Gateway down", "OPNsense gateway degraded", OPERATOR)

    decision = await route_task(
        db_session,
        task,
        OPERATOR,
        source="test",
        context={"provider_id": "opnsense"},
        model=FakeRouterModel(),
    )

    assert decision is not None
    assert decision.category == "network"
    assert decision.suggested_owner == "claude"

    events = (
        await db_session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.kind == "task.router_decision")
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["model"] == "fake-luna"
    assert events[0].payload["decision"]["runbook"] == "gateway_alert"
    assert events[0].payload["decision"]["labels"] == ["network", "gateway-alert"]

    audit = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.task_id == task.id, AuditEvent.action == "task.router_decision")
        )
    ).scalars().all()
    assert len(audit) == 1


async def test_route_task_records_failure_event_and_does_not_block(db_session):
    task = await create_task(db_session, "Cloudflare done", "Completed provider task", OPERATOR)

    decision = await route_task(db_session, task, OPERATOR, source="test", model=FailingRouterModel())

    assert decision is None
    events = (
        await db_session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.kind == "task.router_failed")
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["error"]["code"] == "task_router_failed"
    assert events[0].payload["error"]["message"] == "task router model response incomplete"
    assert events[0].payload["error"]["details"] == {
        "http_status": 200,
        "response_status": "incomplete",
        "incomplete_reason": "max_output_tokens",
        "output_text_length": 417,
        "output_units": 300,
    }

    audit = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.task_id == task.id, AuditEvent.action == "task.router_decision")
        )
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].outcome == "error"


async def test_router_failure_alert_is_grouped_and_success_sends_recovery(db_session, monkeypatch):
    monkeypatch.setenv("TELEGRAM_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_CRITICAL_COOLDOWN_SECONDS", "1800")
    get_settings.cache_clear()

    first = await create_task(db_session, "First failure", "Router guard", OPERATOR)
    second = await create_task(db_session, "Second failure", "Router guard", OPERATOR)
    await route_task(db_session, first, OPERATOR, source="test", model=FailingRouterModel())
    await route_task(db_session, second, OPERATOR, source="test", model=FailingRouterModel())

    failure_alerts = (
        await db_session.execute(
            select(NotificationOutbox).where(NotificationOutbox.event_type == "luna.router.failed")
        )
    ).scalars().all()
    assert len(failure_alerts) == 1
    assert failure_alerts[0].status == "pending"
    assert "Luna Task Router degraded" in failure_alerts[0].text
    assert "incomplete model response" in failure_alerts[0].text

    recovered_task = await create_task(db_session, "Gateway down", "OPNsense gateway degraded", OPERATOR)
    decision = await route_task(
        db_session,
        recovered_task,
        OPERATOR,
        source="test",
        context={"provider_id": "opnsense"},
        model=FakeRouterModel(),
    )
    assert decision is not None

    recovery_alerts = (
        await db_session.execute(
            select(NotificationOutbox).where(NotificationOutbox.event_type == "luna.router.recovered")
        )
    ).scalars().all()
    assert len(recovery_alerts) == 1
    assert "Failures during degradation: 2" in recovery_alerts[0].text

    relapse = await create_task(db_session, "Relapse", "Router guard", OPERATOR)
    await route_task(db_session, relapse, OPERATOR, source="test", model=FailingRouterModel())
    failure_alerts = (
        await db_session.execute(
            select(NotificationOutbox).where(NotificationOutbox.event_type == "luna.router.failed")
        )
    ).scalars().all()
    assert len(failure_alerts) == 2
    assert failure_alerts[-1].status == "pending"

    get_settings.cache_clear()


async def test_route_task_normalizes_out_of_policy_model_values(db_session):
    task = await create_task(db_session, "Unknown", "Something happened", OPERATOR)

    decision = await route_task(db_session, task, OPERATOR, source="test", model=InvalidRouterModel())

    assert decision is not None
    assert decision.action == "operator_review"
    assert decision.category == "unknown"
    assert decision.priority == "medium"
    assert decision.severity == "warning"
    assert decision.suggested_owner == "operator"
