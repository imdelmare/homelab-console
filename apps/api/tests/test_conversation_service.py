import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from app.core.settings import get_settings
from app.db.models import ConversationMessage, LlmUsageEvent, Task
from app.domain.actors import Actor
from app.services.opencode_go import OpenCodeGoError, OpenCodeGoResult
from app.services.conversation_service import (
    ConversationDecision,
    ConversationError,
    ConversationModelResult,
    ConversationToolCall,
    AIManagerWithLunaFallbackModel,
    OpenCodeConversationModel,
    OpenCodeWithAIManagerFallbackModel,
    OllamaWithLunaFallbackModel,
    _conversation_tool_input,
    _fallback_telemetry,
    _instructions,
    _normalize_decision,
    _provider_telemetry,
    _run_allowed_tool,
    _post_openai_response,
    consume_operations_question,
    conversation_status,
    create_operations_question,
    get_conversation_model,
    handle_free_chat_message,
    handle_conversation_message,
    handle_operations_shortcut,
)
import httpx
from app.tools.execution import ExecutionResult
from tests.conftest import do_login

OPERATOR = Actor(kind="telegram", id="111", label="telegram operator")


class FakeModel:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.contexts = []

    async def decide(self, context):
        self.contexts.append(context)
        decision = self.decisions.pop(0)
        return ConversationModelResult(
            decision=decision,
            model="fake-luna",
            input_tokens=100,
            output_tokens=20,
            estimated_cost=0.001,
        )


class FailingModel:
    def __init__(self):
        self.calls = 0

    async def decide(self, context):
        self.calls += 1
        raise ConversationError("model unavailable")


class CountingModel(FakeModel):
    async def decide(self, context):
        return await super().decide(context)


class FakeAsyncClient:
    responses = []
    calls = 0

    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, *args, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        type(self).calls = self.calls
        return response


def _openai_response(status_code: int) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"error": {"code": "temporary", "message": "temporary"}},
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )


async def test_openai_adapter_retries_transient_errors(monkeypatch):
    FakeAsyncClient.responses = [_openai_response(503), _openai_response(200)]
    FakeAsyncClient.calls = 0
    monkeypatch.setattr("app.services.conversation_service.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.services.conversation_service.asyncio.sleep", lambda _delay: _noop())

    response = await _post_openai_response(api_key="test", payload={}, timeout_seconds=1)

    assert response.status_code == 200
    assert FakeAsyncClient.calls == 2


async def test_openai_adapter_does_not_retry_non_transient_errors(monkeypatch):
    FakeAsyncClient.responses = [_openai_response(400), _openai_response(200)]
    FakeAsyncClient.calls = 0
    monkeypatch.setattr("app.services.conversation_service.httpx.AsyncClient", FakeAsyncClient)

    response = await _post_openai_response(api_key="test", payload={}, timeout_seconds=1)

    assert response.status_code == 400
    assert FakeAsyncClient.calls == 1


def test_opencode_provider_selection_and_status(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_provider", "opencode_go")
    monkeypatch.setattr(settings, "opencode_go_api_key", "test-key")

    assert isinstance(get_conversation_model(), OpenCodeWithAIManagerFallbackModel)
    status = conversation_status()
    assert status.configured is True
    assert "opencode-go/deepseek-v4-flash" in status.model

    monkeypatch.setattr(settings, "opencode_go_api_key", "")
    assert conversation_status().configured is False

    monkeypatch.setattr(settings, "opencode_go_api_key", "   ")
    assert conversation_status().configured is False


def test_ai_manager_provider_selection(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_provider", "ai_manager")

    assert isinstance(get_conversation_model(), AIManagerWithLunaFallbackModel)


async def test_opencode_request_contract_and_valid_response(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "opencode_go_api_key", "test-key")
    captured = {}

    async def fake_request(**kwargs):
        captured.update(kwargs)
        return OpenCodeGoResult(
            output_text='{"assistant_reply":"Tutto stabile."}',
            model="deepseek-v4-flash",
            input_tokens=12,
            output_tokens=4,
        )

    monkeypatch.setattr(
        "app.services.conversation_service.request_structured_decision",
        fake_request,
    )

    result = await OpenCodeConversationModel().decide({"user_message": "stato"})

    assert captured["model"] == "deepseek-v4-flash"
    assert captured["context"] == {"user_message": "stato"}
    assert captured["schema"]["additionalProperties"] is False
    assert result.model == "opencode-go/deepseek-v4-flash"
    assert (result.input_tokens, result.output_tokens, result.estimated_cost) == (12, 4, 0.0)


async def test_opencode_requires_direct_api_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "opencode_go_api_key", "")

    with pytest.raises(ConversationError, match="not configured"):
        await OpenCodeConversationModel().decide({})


@pytest.mark.parametrize(
    ("error_kind", "message"),
    [
        ("timeout", "provider failure"),
        ("transport_error", "provider failure"),
        ("http_error", "provider failure"),
        ("invalid_response", "provider failure"),
    ],
)
async def test_opencode_sanitizes_provider_errors(monkeypatch, error_kind, message):
    monkeypatch.setattr(get_settings(), "opencode_go_api_key", "test-key")

    async def fake_request(**_kwargs):
        raise OpenCodeGoError(message, error_kind=error_kind)

    monkeypatch.setattr(
        "app.services.conversation_service.request_structured_decision",
        fake_request,
    )
    with pytest.raises(ConversationError, match=message):
        await OpenCodeConversationModel().decide({})


async def test_ollama_failure_falls_back_to_luna_and_opens_circuit(monkeypatch):
    primary = FailingModel()
    fallback = CountingModel(
        [
            ConversationDecision(assistant_reply="Fallback Luna 1"),
            ConversationDecision(assistant_reply="Fallback Luna 2"),
        ]
    )
    model = OllamaWithLunaFallbackModel(primary=primary, fallback=fallback)
    monkeypatch.setattr("app.services.conversation_service._ollama_unavailable_until", 0.0)

    first = await model.decide({"message": "stato"})
    second = await model.decide({"message": "stato ancora"})

    assert first.decision.assistant_reply == "Fallback Luna 1"
    assert second.decision.assistant_reply == "Fallback Luna 2"
    assert primary.calls == 1
    assert len(fallback.contexts) == 2


async def test_ollama_success_does_not_call_luna(monkeypatch):
    primary = CountingModel([ConversationDecision(assistant_reply="Risposta locale")])
    fallback = CountingModel([ConversationDecision(assistant_reply="Fallback")])
    model = OllamaWithLunaFallbackModel(primary=primary, fallback=fallback)
    monkeypatch.setattr("app.services.conversation_service._ollama_unavailable_until", 0.0)

    result = await model.decide({"message": "stato"})

    assert result.decision.assistant_reply == "Risposta locale"
    assert len(primary.contexts) == 1
    assert fallback.contexts == []


async def test_ai_manager_failure_falls_back_to_luna_and_opens_shared_circuit(monkeypatch):
    primary = FailingModel()
    fallback = CountingModel(
        [
            ConversationDecision(assistant_reply="Fallback Luna 1"),
            ConversationDecision(assistant_reply="Fallback Luna 2"),
        ]
    )
    model = AIManagerWithLunaFallbackModel(primary=primary, fallback=fallback)
    monkeypatch.setattr("app.services.ai_manager._unavailable_until", 0.0)

    first = await model.decide({"message": "stato"})
    second = await model.decide({"message": "stato ancora"})

    assert first.decision.assistant_reply == "Fallback Luna 1"
    assert second.decision.assistant_reply == "Fallback Luna 2"
    assert primary.calls == 1
    assert len(fallback.contexts) == 2


def test_conversation_instructions_require_italian_replies():
    instructions = _instructions()

    assert "sempre" in instructions
    assert "in italiano" in instructions


def test_status_request_removes_unrequested_task_side_effects():
    decision = ConversationDecision(
        tool_calls=[
            ConversationToolCall(tool_id="lab.summary", input={"title": "Stato"}),
            ConversationToolCall(
                tool_id="tasks.create",
                input={"title": "Controllare il lab", "goal": "Verificare lo stato"},
            ),
        ],
        create_task=True,
        task_title="Controllare il lab",
        task_goal="Verificare lo stato",
        update_task_id="task-123",
        update_task_summary="Controllo avviato",
    )

    normalized = _normalize_decision(
        decision,
        "Controlla lo stato del laboratorio",
        current_task_id=None,
    )

    assert [call.tool_id for call in normalized.tool_calls] == ["lab.summary"]
    assert _conversation_tool_input(normalized.tool_calls[0]) == {}
    assert normalized.create_task is False
    assert normalized.task_title is None
    assert normalized.update_task_id is None


def test_explicit_task_creation_is_preserved_and_cannot_also_update():
    decision = ConversationDecision(
        tool_calls=[
            ConversationToolCall(
                tool_id="tasks.create",
                input={
                    "title": "Controllare la rete",
                    "goal": "Verificare la latenza",
                },
            )
        ],
        create_task=True,
        task_title="Controllare la rete",
        task_goal="Verificare la latenza",
        update_task_id="task-123",
        update_task_summary="Aggiornamento improprio",
    )

    normalized = _normalize_decision(
        decision,
        "Apri una task per controllare la rete",
        current_task_id="task-123",
    )

    assert normalized.create_task is True
    assert normalized.task_title == "Controllare la rete"
    assert normalized.tool_calls == []
    assert normalized.update_task_id is None
    assert normalized.update_task_summary is None


def test_clarification_removes_all_tools_and_task_mutations():
    decision = ConversationDecision(
        assistant_reply="Vuoi che controlli la rete?",
        tool_calls=[
            ConversationToolCall(tool_id="lab.network.summary"),
            ConversationToolCall(
                tool_id="tasks.create",
                input={"title": "Controllare la rete"},
            ),
        ],
        create_task=True,
        task_title="Controllare la rete",
        needs_clarification=True,
    )

    normalized = _normalize_decision(
        decision,
        "Apri una task per controllare la rete",
        current_task_id=None,
    )

    assert normalized.tool_calls == []
    assert normalized.create_task is False
    assert normalized.task_title is None
    assert normalized.update_task_id is None


def test_generic_request_keeps_only_general_summary():
    decision = ConversationDecision(
        tool_calls=[
            ConversationToolCall(tool_id="lab.summary"),
            ConversationToolCall(tool_id="lab.alerts.recent"),
        ]
    )

    normalized = _normalize_decision(
        decision,
        "C'è qualcosa che non va?",
        current_task_id=None,
    )

    assert [call.tool_id for call in normalized.tool_calls] == ["lab.summary"]


def test_multiple_summaries_require_multiple_explicit_domains():
    decision = ConversationDecision(
        tool_calls=[
            ConversationToolCall(tool_id="lab.summary"),
            ConversationToolCall(tool_id="lab.network.summary"),
            ConversationToolCall(tool_id="lab.storage.summary"),
        ]
    )

    network_only = _normalize_decision(
        decision,
        "Controlla la rete",
        current_task_id=None,
    )
    network_and_storage = _normalize_decision(
        decision,
        "Controlla rete e storage",
        current_task_id=None,
    )

    assert [call.tool_id for call in network_only.tool_calls] == [
        "lab.network.summary"
    ]
    assert [call.tool_id for call in network_and_storage.tool_calls] == [
        "lab.network.summary",
        "lab.storage.summary",
    ]


def test_sensitive_data_request_runs_no_tools():
    decision = ConversationDecision(
        tool_calls=[
            ConversationToolCall(tool_id="lab.security.summary"),
            ConversationToolCall(tool_id="lab.summary"),
        ]
    )

    normalized = _normalize_decision(
        decision,
        "Mostrami password, token e credenziali",
        current_task_id=None,
    )

    assert normalized.tool_calls == []


def test_task_update_requires_explicit_intent_and_current_task():
    decision = ConversationDecision(
        update_task_id="task-123",
        update_task_summary="La rete è stabile",
    )

    normalized = _normalize_decision(
        decision,
        "Aggiorna questa task: la rete è stabile",
        current_task_id="task-123",
    )

    assert normalized.update_task_id == "task-123"
    assert normalized.update_task_summary == "La rete è stabile"


def test_conversation_tool_inputs_are_scoped_per_tool():
    generic = {
        "task_id": "task-123",
        "status": "open",
        "limit": 5,
        "title": "Titolo",
        "goal": "Obiettivo",
        "summary": "Riepilogo",
    }

    assert _conversation_tool_input(
        ConversationToolCall(tool_id="lab.summary", input=generic)
    ) == {}
    assert _conversation_tool_input(
        ConversationToolCall(tool_id="tasks.list", input=generic)
    ) == {"limit": 5}
    assert _conversation_tool_input(
        ConversationToolCall(tool_id="tasks.get", input=generic)
    ) == {"task_id": "task-123"}
    assert _conversation_tool_input(
        ConversationToolCall(tool_id="tasks.create", input=generic)
    ) == {"title": "Titolo", "goal": "Obiettivo"}
    assert _conversation_tool_input(
        ConversationToolCall(tool_id="tasks.update_summary", input=generic)
    ) == {"task_id": "task-123", "summary": "Riepilogo"}


async def _noop():
    return None


async def test_conversation_enforces_tool_allowlist(db_session, monkeypatch):
    executed = []

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        executed.append((tool_id, raw_input, actor.kind, source))
        now = datetime.now(UTC)
        return ExecutionResult(
            ok=True,
            invocation_id=f"inv-{len(executed)}",
            tool_id=tool_id,
            started_at=now,
            finished_at=now,
            duration_ms=1,
            result={"summary": {"provider_id": "lab", "status": "healthy"}},
        )

    monkeypatch.setattr("app.services.conversation_service.execute_tool", fake_execute_tool)
    model = FakeModel(
        [
            ConversationDecision(
                tool_calls=[
                    ConversationToolCall(tool_id="proxmox.version"),
                    ConversationToolCall(tool_id="lab.summary"),
                    ConversationToolCall(tool_id="lab.network.summary"),
                ],
            ),
            ConversationDecision(assistant_reply="Il lab sembra stabile."),
        ]
    )

    result = await handle_conversation_message(
        db_session,
        channel="telegram",
        user_ref="111",
        content="Come sta il lab?",
        actor=OPERATOR,
        model=model,
    )

    assert result.assistant_reply == "Il lab sembra stabile."
    assert [item[0] for item in executed] == ["lab.summary"]
    assert result.tool_results[0]["ok"] is False
    assert result.tool_results[0]["error"] == "tool_not_allowed"
    assert model.contexts[1]["allowed_tools"] == []


async def test_summary_tool_discards_generic_model_inputs(db_session, monkeypatch):
    executed = []

    async def fake_execute_tool(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        executed.append((tool_id, raw_input))
        now = datetime.now(UTC)
        return ExecutionResult(
            ok=True,
            invocation_id="inv-summary",
            tool_id=tool_id,
            started_at=now,
            finished_at=now,
            duration_ms=1,
            result={"status": "healthy"},
        )

    monkeypatch.setattr("app.services.conversation_service.execute_tool", fake_execute_tool)
    call = ConversationToolCall(
        tool_id="lab.summary",
        input={
            "status": "operational",
            "limit": 100,
            "title": "Overall Lab Status",
            "goal": "Summarize",
            "summary": "",
        },
    )

    result = await _run_allowed_tool(db_session, call, OPERATOR, task_id=None)

    assert result["ok"] is True
    assert executed == [("lab.summary", {})]


async def test_free_chat_has_no_tools_or_task_side_effects(db_session, monkeypatch):
    async def fail_execute(*_args, **_kwargs):
        raise AssertionError("free chat must not execute tools")

    monkeypatch.setattr("app.services.conversation_service.execute_tool", fail_execute)
    model = FakeModel(
        [
            ConversationDecision(
                assistant_reply="Possiamo ragionarci insieme.",
                tool_calls=[ConversationToolCall(tool_id="lab.summary")],
                create_task=True,
                task_title="Should not exist",
                task_goal="Free chat cannot create tasks",
            )
        ]
    )

    result = await handle_free_chat_message(
        db_session,
        channel="telegram",
        user_ref="111",
        content="Spiegami come funziona WireGuard",
        actor=OPERATOR,
        model=model,
    )

    assert result.assistant_reply == "Possiamo ragionarci insieme."
    assert model.contexts[0]["mode"] == "chat"
    assert model.contexts[0]["allowed_tools"] == []
    assert model.contexts[0]["current_task"] is None
    tasks = (await db_session.execute(select(Task).where(Task.title == "Should not exist"))).scalars().all()
    assert tasks == []


async def test_operations_shortcut_runs_fixed_tool_then_one_model(db_session, monkeypatch):
    executed = []

    async def fake_execute(tool_id, raw_input, actor, task_id=None, approval_id=None, source=None):
        executed.append((tool_id, raw_input, actor.kind, source))
        now = datetime.now(UTC)
        return ExecutionResult(
            ok=True,
            invocation_id="inv-shortcut",
            tool_id=tool_id,
            started_at=now,
            finished_at=now,
            duration_ms=2,
            result={"status": "healthy"},
        )

    monkeypatch.setattr("app.services.conversation_service.execute_tool", fake_execute)
    model = FakeModel([ConversationDecision(assistant_reply="La rete risulta stabile.")])

    result = await handle_operations_shortcut(
        db_session,
        channel="telegram",
        user_ref="111",
        tool_id="lab.network.summary",
        label="Network",
        actor=OPERATOR,
        model=model,
    )

    assert result.assistant_reply == "La rete risulta stabile."
    assert executed == [("lab.network.summary", {}, "telegram", "conversation")]
    assert len(model.contexts) == 1
    assert model.contexts[0]["mode"] == "operations_shortcut"
    assert model.contexts[0]["allowed_tools"] == []
    assert model.contexts[0]["tool_results"][0]["tool_id"] == "lab.network.summary"


async def test_operations_question_is_bound_and_single_use(db_session):
    nonce = await create_operations_question(
        db_session,
        channel="telegram",
        user_ref="111",
        actor=OPERATOR,
    )

    conversation_id = await consume_operations_question(
        db_session,
        channel="telegram",
        user_ref="111",
        nonce=nonce,
        actor=OPERATOR,
    )

    assert conversation_id
    with pytest.raises(ConversationError, match="expired"):
        await consume_operations_question(
            db_session,
            channel="telegram",
            user_ref="111",
            nonce=nonce,
            actor=OPERATOR,
        )


async def test_conversation_stores_messages_usage_and_creates_task(db_session):
    model = FakeModel(
        [
            ConversationDecision(
                assistant_reply="Ho aperto una task.",
                create_task=True,
                task_title="Internet lento",
                task_goal="Verificare latenza e stato rete dal riepilogo lab.",
            )
        ]
    )

    result = await handle_conversation_message(
        db_session,
        channel="telegram",
        user_ref="111",
        content="Apri una task per internet lento",
        actor=OPERATOR,
        model=model,
    )
    await db_session.commit()

    assert result.created_task_id
    task = await db_session.get(Task, result.created_task_id)
    assert task is not None
    assert task.title == "Internet lento"
    assert task.created_by == "telegram:111"

    messages = (
        await db_session.execute(
            select(ConversationMessage).where(ConversationMessage.conversation_id == result.conversation_id)
        )
    ).scalars().all()
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[-1].model == "fake-luna"
    assert messages[-1].input_tokens == 100
    assert messages[-1].output_tokens == 20
    assert messages[-1].estimated_cost == 0.001


async def test_task_creation_alias_is_canonicalized_without_duplicates(db_session):
    model = FakeModel(
        [
            ConversationDecision(
                tool_calls=[
                    ConversationToolCall(
                        tool_id="tasks.create",
                        input={"title": "Internet lento", "goal": "Verificare latenza e stato rete."},
                    )
                ],
                create_task=True,
                task_title="Internet lento",
                task_goal="Verificare latenza e stato rete.",
                assistant_reply="Task creata.",
            ),
        ]
    )

    result = await handle_conversation_message(
        db_session,
        channel="web",
        user_ref="operator",
        content="Apri una task per internet lento",
        actor=Actor(kind="user", id="operator", label="operator"),
        model=model,
    )

    await db_session.commit()

    assert result.tool_results == []
    assert result.created_task_id
    rows = (await db_session.execute(select(Task))).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "Internet lento"


async def test_conversation_ignores_unrequested_task_proposal(db_session):
    model = FakeModel(
        [
            ConversationDecision(
                assistant_reply="Vedo un problema. Vuoi che apra una task?",
                create_task=True,
                task_title="Verificare rete",
                task_goal="Controllare lo stato rete dai summary.",
            )
        ]
    )

    result = await handle_conversation_message(
        db_session,
        channel="telegram",
        user_ref="111",
        content="Internet sembra lento",
        actor=OPERATOR,
        model=model,
    )
    assert result.created_task_id is None
    assert result.pending_task_nonce is None
    assert result.pending_task_title is None
    rows = (await db_session.execute(select(Task))).scalars().all()
    assert rows == []


async def test_web_conversation_endpoint_uses_shared_service(client, user, capture_adapter, monkeypatch):
    model = FakeModel([ConversationDecision(assistant_reply="Tutto stabile.")])
    monkeypatch.setattr("app.services.conversation_service.get_conversation_model", lambda: model)
    _, csrf = await do_login(client, capture_adapter)

    response = await client.post(
        "/api/conversations/message",
        json={"message": "Come sta il mio homelab?"},
        headers={"x-csrf-token": csrf},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assistant_reply"] == "Tutto stabile."
    assert body["conversation_id"]


async def test_web_conversation_status_endpoint(client, user, capture_adapter):
    _, csrf = await do_login(client, capture_adapter)

    response = await client.get("/api/conversations/status", headers={"x-csrf-token": csrf})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"]
    assert body["max_tool_calls"] == 3


async def test_conversation_gate_disables_status_and_rejects_before_persistence(
    db_session, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_enabled", False)
    model = FailingModel()

    status = conversation_status()
    assert status.enabled is False
    assert status.configured is False
    assert status.reason == "conversation_disabled"
    assert status.model == ""

    with pytest.raises(ConversationError) as exc_info:
        await handle_conversation_message(
            db_session,
            channel="web",
            user_ref="operator",
            content="Stato del lab",
            actor=OPERATOR,
            model=model,
        )

    assert exc_info.value.code == "conversation_disabled"
    assert model.calls == 0
    assert (await db_session.execute(select(ConversationMessage))).scalars().all() == []


async def test_opencode_fallback_success_does_not_call_ai_manager(monkeypatch):
    primary = CountingModel([ConversationDecision(assistant_reply="OpenCode reply")])
    fallback = CountingModel([ConversationDecision(assistant_reply="AI Manager fallback")])
    model = OpenCodeWithAIManagerFallbackModel(primary=primary, fallback=fallback)

    result = await model.decide({"message": "stato"})

    assert result.decision.assistant_reply == "OpenCode reply"
    assert len(primary.contexts) == 1
    assert fallback.contexts == []
    assert result.telemetry["fallback_used"] is False
    assert result.telemetry["provider"] == "opencode_go"


async def test_opencode_failure_falls_back_to_ai_manager():
    primary = FailingModel()
    decisions = [ConversationDecision(assistant_reply="AI Manager fallback")]
    fallback = CountingModel(decisions)
    model = OpenCodeWithAIManagerFallbackModel(primary=primary, fallback=fallback)

    result = await model.decide({"message": "stato"})

    assert result.decision.assistant_reply == "AI Manager fallback"
    assert primary.calls == 1
    assert len(fallback.contexts) == 1
    assert result.telemetry["fallback_used"] is True
    assert result.telemetry["fallback_reason"].startswith("opencode_go:")


async def test_opencode_and_fallback_failure_preserve_chain():
    class TelemetryFailure:
        def __init__(self, provider: str, error_kind: str):
            self.provider = provider
            self.error_kind = error_kind

        async def decide(self, _context):
            raise ConversationError(
                f"{self.provider} unavailable",
                telemetry={
                    "provider": self.provider,
                    "error_kind": self.error_kind,
                    "inference_latency_ms": 10,
                },
            )

    model = OpenCodeWithAIManagerFallbackModel(
        primary=TelemetryFailure("opencode_go", "timeout"),
        fallback=TelemetryFailure("ai_manager", "transport"),
    )

    with pytest.raises(ConversationError) as exc_info:
        await model.decide({"message": "stato"})

    assert exc_info.value.telemetry["provider"] == "ai_manager"
    assert exc_info.value.telemetry["fallback_used"] is True
    assert exc_info.value.telemetry["fallback_reason"] == "opencode_go:timeout;ai_manager:transport"
    assert exc_info.value.telemetry["inference_latency_ms"] == 20


async def test_failed_conversation_records_delivery_event(db_session):
    with pytest.raises(ConversationError, match="model unavailable"):
        await handle_conversation_message(
            db_session,
            channel="telegram",
            user_ref="111",
            content="stato",
            actor=OPERATOR,
            model=FailingModel(),
        )

    event = (
        await db_session.execute(
            select(LlmUsageEvent)
            .where(LlmUsageEvent.component == "conversation")
            .order_by(LlmUsageEvent.created_at.desc())
        )
    ).scalars().first()
    assert event is not None
    assert event.status == "error"
    assert event.model == "unknown"


async def test_second_model_failure_rolls_back_tool_turn(db_session):
    class FailAfterToolModel:
        def __init__(self):
            self.calls = 0

        async def decide(self, _context):
            self.calls += 1
            if self.calls == 1:
                return ConversationModelResult(
                    decision=ConversationDecision(
                        assistant_reply="",
                        tool_calls=[
                            ConversationToolCall(
                                tool_id="tasks.list",
                                input={"limit": 1},
                            )
                        ],
                    ),
                    model="fake-luna",
                    telemetry={"provider": "openai", "inference_latency_ms": 10},
                )
            raise ConversationError(
                "second decision failed",
                telemetry={"provider": "openai", "inference_latency_ms": 20},
            )

    with pytest.raises(ConversationError, match="second decision failed"):
        await handle_conversation_message(
            db_session,
            channel="telegram",
            user_ref="111",
            content="elenca le task aperte",
            actor=OPERATOR,
            model=FailAfterToolModel(),
        )
    await db_session.commit()

    tool_messages = (
        await db_session.execute(
            select(ConversationMessage).where(ConversationMessage.role == "tool")
        )
    ).scalars().all()
    assert tool_messages == []
    event = (
        await db_session.execute(
            select(LlmUsageEvent)
            .where(LlmUsageEvent.component == "conversation")
            .order_by(LlmUsageEvent.created_at.desc())
        )
    ).scalars().first()
    assert event is not None
    assert event.status == "error"
    assert event.inference_latency_ms == 30


def test_provider_telemetry_with_success():
    telemetry = _provider_telemetry("opencode", 0.0)

    assert telemetry["provider"] == "opencode"
    assert telemetry["fallback_used"] is False
    assert telemetry["error_kind"] == ""
    assert isinstance(telemetry["inference_latency_ms"], int)
    assert telemetry["prompt_version"]


def test_provider_telemetry_with_error_kind():
    telemetry = _provider_telemetry("openai", 1000.0, error_kind="timeout")

    assert telemetry["provider"] == "openai"
    assert telemetry["error_kind"] == "timeout"
    assert telemetry["inference_latency_ms"] >= 0


def test_fallback_telemetry_combines_providers():
    primary = {"provider": "opencode", "error_kind": "timeout", "inference_latency_ms": 100}
    fallback = {"provider": "ai_manager", "inference_latency_ms": 200, "fallback_used": False}

    result = _fallback_telemetry(primary, fallback, primary_provider="opencode")

    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "opencode:timeout"
    assert result["inference_latency_ms"] == 300


def test_fallback_telemetry_chains_nested_reason():
    primary = {"provider": "opencode", "error_kind": "transport", "inference_latency_ms": 50}
    fallback = {
        "provider": "openai",
        "fallback_used": True,
        "fallback_reason": "ai_manager:timeout;luna:http_error",
        "inference_latency_ms": 150,
    }

    result = _fallback_telemetry(primary, fallback, primary_provider="opencode")

    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "opencode:transport;ai_manager:timeout;luna:http_error"
    assert result["inference_latency_ms"] == 200


def test_conversation_status_opencode_shows_full_chain(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_provider", "opencode_go")
    monkeypatch.setattr(settings, "opencode_go_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_manager_model", "Qwen3.5-4B-Q8_0")
    monkeypatch.setattr(settings, "conversation_model", "gpt-5.6-luna")

    status = conversation_status()

    assert "opencode-go/deepseek-v4-flash" in status.model
    assert "ai-manager" in status.model
    assert "luna" in status.model


def test_conversation_status_opencode_configured_with_fallback_only(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_provider", "opencode_go")
    monkeypatch.setattr(settings, "opencode_go_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "key")
    monkeypatch.setattr(settings, "conversation_model", "gpt-5.6-luna")
    monkeypatch.setattr("app.services.conversation_service.ai_manager_configured", lambda: True)

    status = conversation_status()

    assert status.configured is True
