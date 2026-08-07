from datetime import timedelta
from types import SimpleNamespace

from app.db.models import Approval, utcnow
from app.db.session import get_session_factory
from app.services.mcp_clients import start_pairing
from app.services.conversation_service import ConversationDecision, ConversationModelResult
from app.services.telegram_service import (
    _home_keyboard,
    _operations_keyboard,
    edit_message_result,
    handle_update,
)
from app.services.task_notifications import _task_keyboard

WEBHOOK_HEADERS = {"x-telegram-bot-api-secret-token": "test-webhook-secret"}


def test_system_navigation_uses_english_labels():
    labels = [button["text"] for row in _home_keyboard() for button in row]

    assert labels == ["🚨 Incidents", "📋 Tasks", "🛰 Operations", "••• More"]


def test_operations_navigation_is_bounded_and_explicit():
    callbacks = [
        button["callback_data"]
        for row in _operations_keyboard()
        for button in row
    ]

    assert callbacks == [
        "operations:summary:overview",
        "operations:summary:network",
        "operations:summary:storage",
        "operations:summary:security",
        "operations:summary:automation",
        "operations:summary:alerts",
        "luna:triage",
        "operations:ask",
        "nav:home",
    ]


def _message(text, user_id="111", chat_id="222"):
    return {"message": {"text": text, "from": {"id": user_id}, "chat": {"id": chat_id}}}


def _photo_message(user_id="111", chat_id="222"):
    return {
        "message": {
            "caption": "Cosa vedi?",
            "photo": [
                {"file_id": "small-photo", "file_size": 100},
                {"file_id": "large-photo", "file_size": 1000},
            ],
            "from": {"id": user_id},
            "chat": {"id": chat_id},
        }
    }


def _voice_message(user_id="111", chat_id="222"):
    return {
        "message": {
            "voice": {
                "file_id": "voice-file",
                "file_size": 2000,
                "duration": 4,
                "mime_type": "audio/ogg",
            },
            "from": {"id": user_id},
            "chat": {"id": chat_id},
        }
    }


class FakeConversationModel:
    async def decide(self, context):
        return ConversationModelResult(
            decision=ConversationDecision(
                assistant_reply="Conviene aprire una task.",
                create_task=True,
                task_title="Verificare rete",
                task_goal="Controllare summary rete.",
            ),
            model="fake-luna",
        )


class FakeMonitorModel:
    async def decide(self, context):
        return ConversationModelResult(
            decision=ConversationDecision(assistant_reply="Triage: controlla prima i watcher critici."),
            model="fake-luna",
        )


async def test_webhook_requires_secret(client):
    response = await client.post("/api/telegram/webhook", json=_message("/status"))
    assert response.status_code == 403


async def test_unauthorized_user_ignored(db_session):
    reply = await handle_update(db_session, _message("/status", user_id="999"))
    assert reply is None


async def test_unauthorized_chat_ignored(db_session):
    reply = await handle_update(db_session, _message("/status", chat_id="999"))
    assert reply is None


async def test_telegram_photo_uses_local_analysis_before_conversation(db_session, monkeypatch):
    captured = {}

    async def fake_analyze(media, caption):
        captured["media"] = media
        captured["caption"] = caption
        return "Messaggio con foto\nAnalisi locale: un gatto arancione."

    async def fake_conversation(_db, **kwargs):
        captured["content"] = kwargs["content"]
        return SimpleNamespace(pending_task_nonce=None, assistant_reply="Vedo un gatto.")

    monkeypatch.setattr("app.services.telegram_service._analyze_telegram_media", fake_analyze)
    monkeypatch.setattr("app.services.telegram_service.handle_free_chat_message", fake_conversation)

    reply = await handle_update(db_session, _photo_message())

    assert isinstance(reply, dict)
    assert reply["text"] == "Vedo un gatto."
    assert captured["media"]["file_id"] == "large-photo"
    assert captured["caption"] == "Cosa vedi?"
    assert "gatto arancione" in captured["content"]


async def test_telegram_voice_uses_local_transcript_before_conversation(db_session, monkeypatch):
    captured = {}

    async def fake_analyze(media, caption):
        captured["media"] = media
        return "Messaggio vocale trascritto localmente:\nCome sta il laboratorio?"

    async def fake_conversation(_db, **kwargs):
        captured["content"] = kwargs["content"]
        return SimpleNamespace(pending_task_nonce=None, assistant_reply="Il laboratorio è stabile.")

    monkeypatch.setattr("app.services.telegram_service._analyze_telegram_media", fake_analyze)
    monkeypatch.setattr("app.services.telegram_service.handle_free_chat_message", fake_conversation)

    reply = await handle_update(db_session, _voice_message())

    assert isinstance(reply, dict)
    assert reply["text"] == "Il laboratorio è stabile."
    assert captured["media"]["kind"] == "voice"
    assert "Come sta il laboratorio?" in captured["content"]


async def test_unauthorized_media_is_not_analyzed(db_session, monkeypatch):
    called = False

    async def fake_analyze(_media, _caption):
        nonlocal called
        called = True
        return "should not run"

    monkeypatch.setattr("app.services.telegram_service._analyze_telegram_media", fake_analyze)

    reply = await handle_update(db_session, _voice_message(user_id="999"))

    assert reply is None
    assert called is False


async def test_provider_switch_from_telegram(client, db_session, monkeypatch):
    sent = {}

    async def fake_send_reply(chat_id, reply):
        sent["chat_id"] = chat_id
        sent["reply"] = reply
        return True

    monkeypatch.setattr("app.api.routes_telegram._send_reply", fake_send_reply)

    response = await client.post(
        "/api/telegram/webhook", json=_message("/provider codex"), headers=WEBHOOK_HEADERS
    )
    assert response.status_code == 200
    assert response.json() == {}
    assert sent["chat_id"] == "222"
    assert "codex" in sent["reply"]

    from app.services.model_providers import get_active_model

    async with get_session_factory()() as db:
        active = await get_active_model(db)
        assert active is not None
        assert active.id == "codex"


async def test_provider_switch_invalid_provider(db_session):
    reply = await handle_update(db_session, _message("/provider skynet"))
    assert isinstance(reply, str)
    assert "Unknown model provider" in reply


async def test_telegram_cannot_run_arbitrary_tools(db_session):
    reply = await handle_update(
        db_session, _message('/run proxmox.version {"x": 1}')
    )
    assert isinstance(reply, dict)
    assert "Unknown command" in reply["text"]
    assert reply["reply_markup"]["inline_keyboard"]


async def test_telegram_menu_has_quick_actions(db_session, monkeypatch):
    async def fake_provider_health_snapshot(_db):
        return []

    monkeypatch.setattr(
        "app.providers.registry.provider_health_snapshot",
        fake_provider_health_snapshot,
    )
    reply = await handle_update(db_session, _message("/menu"))
    assert isinstance(reply, dict)

    assert "🏠 Homelab" in reply["text"]
    buttons = [button["callback_data"] for row in reply["reply_markup"]["inline_keyboard"] for button in row]
    assert len(buttons) == 4
    assert "nav:incidents" in buttons
    assert "nav:tasks" in buttons
    assert "nav:operations" in buttons
    assert "nav:more" in buttons


async def test_telegram_more_keeps_secondary_actions_out_of_home(db_session):
    reply = await handle_update(
        db_session,
        {
            "callback_query": {
                "from": {"id": "111"},
                "message": {"chat": {"id": "222"}},
                "data": "nav:more",
            }
        },
    )
    assert isinstance(reply, dict)

    buttons = [button["callback_data"] for row in reply["reply_markup"]["inline_keyboard"] for button in row]
    assert buttons == ["nav:watchers", "nav:mcp", "nav:status", "nav:home"]


async def test_telegram_tasks_callback_lists_open_tasks(db_session):
    from app.domain.actors import Actor
    from app.services.tasks_service import create_task

    await create_task(
        db_session,
        "Telegram visible task",
        "Show this task in Telegram",
        Actor(kind="telegram", id="111", label="telegram operator"),
        source="watcher",
    )
    reply = await handle_update(
        db_session,
        {
            "callback_query": {
                "from": {"id": "111"},
                "message": {"chat": {"id": "222"}},
                "data": "nav:tasks",
            }
        },
    )
    assert isinstance(reply, dict)

    assert "Open tasks" in reply["text"]
    assert "Telegram visible task" in reply["text"]
    buttons = [button["callback_data"] for row in reply["reply_markup"]["inline_keyboard"] for button in row]
    assert any(item.startswith("task:detail:") for item in buttons)


async def test_new_task_notification_offers_manual_fixer_assignment():
    keyboard = _task_keyboard("task-12345678")
    assert keyboard is not None
    assert keyboard["inline_keyboard"][0][0]["text"] == "Open task"
    buttons = [
        button["callback_data"]
        for row in keyboard["inline_keyboard"]
        for button in row
    ]
    assert "task:assign_fixer:task-12345678" in buttons


async def test_telegram_operator_can_assign_task_to_fixer(db_session):
    from app.domain.actors import Actor
    from app.services.tasks_service import create_task, get_task

    task = await create_task(
        db_session,
        "Fix lab service",
        "Operator dispatches this task manually.",
        Actor(kind="telegram", id="111", label="telegram operator"),
        source="rest",
    )

    reply = await handle_update(
        db_session,
        {
            "callback_query": {
                "from": {"id": "111"},
                "message": {"chat": {"id": "222"}},
                "data": f"task:assign_fixer:{task.id}",
            }
        },
    )

    assigned = await get_task(db_session, task.id)
    assert assigned is not None
    assert isinstance(reply, dict)
    assert assigned.status == "claimed"
    assert assigned.assigned_agent == "agent:fixer"
    assert "Task assigned, startup failed (dispatch_disabled)" in reply["text"]


async def test_telegram_luna_triage_callback_uses_conversation_service(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.conversation_service.get_conversation_model",
        lambda: FakeMonitorModel(),
    )
    reply = await handle_update(
        db_session,
        {
            "callback_query": {
                "from": {"id": "111"},
                "message": {"chat": {"id": "222"}},
                "data": "luna:triage",
            }
        },
    )
    assert isinstance(reply, dict)

    assert "Triage" in reply["text"]
    buttons = [button["callback_data"] for row in reply["reply_markup"]["inline_keyboard"] for button in row]
    assert "luna:summary" in buttons
    assert "luna:triage" in buttons


async def test_telegram_free_text_does_not_propose_unrequested_task(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.conversation_service.get_conversation_model",
        lambda: FakeConversationModel(),
    )

    reply = await handle_update(db_session, _message("Internet sembra lento"))
    assert isinstance(reply, dict)
    assert reply["text"] == "Conviene aprire una task."
    callbacks = [
        button["callback_data"]
        for row in reply["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert callbacks == ["nav:operations", "nav:home"]


async def test_operations_shortcut_uses_fixed_summary_tool(db_session, monkeypatch):
    captured = {}

    async def fake_shortcut(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(assistant_reply="La rete è stabile.", model="fake-luna")

    monkeypatch.setattr("app.services.telegram_service.handle_operations_shortcut", fake_shortcut)

    reply = await handle_update(
        db_session,
        {
            "callback_query": {
                "from": {"id": "111"},
                "message": {"chat": {"id": "222"}},
                "data": "operations:summary:network",
            }
        },
    )

    assert isinstance(reply, dict)
    assert reply["text"] == "La rete è stabile."
    assert captured["tool_id"] == "lab.network.summary"
    assert captured["label"] == "Network"


async def test_operations_question_is_one_shot_force_reply(db_session, monkeypatch):
    prompt = await handle_update(
        db_session,
        {
            "callback_query": {
                "from": {"id": "111"},
                "message": {"chat": {"id": "222"}},
                "data": "operations:ask",
            }
        },
    )
    assert isinstance(prompt, dict)
    assert prompt["send_new"] is True
    assert prompt["reply_markup"]["force_reply"] is True
    assert "[OPS:" in prompt["text"]

    captured = {}

    async def fake_operations(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            pending_task_nonce=None,
            assistant_reply="La rete live è stabile.",
            model="fake-luna",
        )

    monkeypatch.setattr("app.services.telegram_service.handle_conversation_message", fake_operations)
    reply = await handle_update(
        db_session,
        {
            "message": {
                "text": "Come sta la rete?",
                "from": {"id": "111"},
                "chat": {"id": "222"},
                "reply_to_message": {"text": prompt["text"]},
            }
        },
    )

    assert isinstance(reply, dict)
    assert reply["text"] == "La rete live è stabile."
    assert captured["conversation_id"]
    assert captured["content"] == "Come sta la rete?"


async def test_webhook_deduplicates_telegram_update_id(client, monkeypatch):
    sends = 0
    edits = 0

    async def fake_send(_chat_id, _text, _reply_markup=None):
        nonlocal sends
        sends += 1
        return True, "progress-1", ""

    async def fake_edit(_chat_id, _message_id, _text, _reply_markup=None):
        nonlocal edits
        edits += 1
        return True, ""

    monkeypatch.setattr(
        "app.services.conversation_service.get_conversation_model",
        lambda: FakeConversationModel(),
    )
    monkeypatch.setattr("app.api.routes_telegram.send_message_result", fake_send)
    monkeypatch.setattr("app.api.routes_telegram.edit_message_result", fake_edit)
    update = {"update_id": 900001, **_message("Internet sembra lento")}

    first = await client.post("/api/telegram/webhook", json=update, headers=WEBHOOK_HEADERS)
    replay = await client.post("/api/telegram/webhook", json=update, headers=WEBHOOK_HEADERS)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert sends == 1
    assert edits == 0


async def test_webhook_edits_callback_message_in_place(client, monkeypatch):
    sends = 0
    edited = {}
    answered = []

    async def fake_send(_chat_id, _text, _reply_markup=None):
        nonlocal sends
        sends += 1
        return True, "new-message", ""

    async def fake_edit(chat_id, message_id, text, reply_markup=None):
        edited.update(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
        return True, ""

    async def fake_answer(callback_query_id):
        answered.append(callback_query_id)
        return True

    monkeypatch.setattr("app.api.routes_telegram.send_message_result", fake_send)
    monkeypatch.setattr("app.api.routes_telegram.edit_message_result", fake_edit)
    monkeypatch.setattr("app.api.routes_telegram.answer_callback_query_result", fake_answer)
    update = {
        "update_id": 900003,
        "callback_query": {
            "id": "callback-77",
            "from": {"id": "111"},
            "message": {"message_id": 77, "chat": {"id": "222"}},
            "data": "nav:more",
        },
    }

    response = await client.post("/api/telegram/webhook", json=update, headers=WEBHOOK_HEADERS)

    assert response.status_code == 200
    assert sends == 0
    assert answered == ["callback-77"]
    assert edited["chat_id"] == "222"
    assert edited["message_id"] == "77"
    assert "More" in edited["text"]
    assert edited["reply_markup"]["inline_keyboard"]


async def test_webhook_sends_force_reply_as_new_message(client, monkeypatch):
    sent = {}
    edits = 0

    async def fake_handle(_db, _update):
        return {
            "text": "Ask live question\n\n[OPS:testnonce]",
            "reply_markup": {"force_reply": True},
            "send_new": True,
        }

    async def fake_send(chat_id, text, reply_markup=None):
        sent.update(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return True, "new-message", ""

    async def fake_edit(*_args, **_kwargs):
        nonlocal edits
        edits += 1
        return True, ""

    monkeypatch.setattr("app.api.routes_telegram.handle_update", fake_handle)
    monkeypatch.setattr("app.api.routes_telegram.send_message_result", fake_send)
    monkeypatch.setattr("app.api.routes_telegram.edit_message_result", fake_edit)
    response = await client.post(
        "/api/telegram/webhook",
        json={
            "update_id": 900004,
            "callback_query": {
                "id": "callback-ops",
                "from": {"id": "111"},
                "message": {"message_id": 77, "chat": {"id": "222"}},
                "data": "operations:ask",
            },
        },
        headers=WEBHOOK_HEADERS,
    )

    assert response.status_code == 200
    assert edits == 0
    assert sent["chat_id"] == "222"
    assert sent["reply_markup"] == {"force_reply": True}


async def test_edit_message_treats_already_applied_edit_as_success(monkeypatch):
    class Response:
        status_code = 400

        @staticmethod
        def json():
            return {"description": "Bad Request: message is not modified"}

    class Client:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, json):
            return Response()

    monkeypatch.setattr(
        "app.services.telegram_service.get_settings",
        lambda: SimpleNamespace(telegram_bot_token="test-token"),
    )
    monkeypatch.setattr("app.services.telegram_service.httpx.AsyncClient", Client)

    ok, error = await edit_message_result("222", "77", "Already applied")

    assert ok is True
    assert error == ""


async def test_webhook_retries_delivery_without_reprocessing_update(client, monkeypatch):
    model_calls = 0
    send_calls = 0

    async def fake_conversation(_db, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        return SimpleNamespace(
            pending_task_nonce=None,
            assistant_reply="Risposta pronta.",
            model="fake-model",
        )

    async def flaky_send(_chat_id, _text, _reply_markup=None):
        nonlocal send_calls
        send_calls += 1
        # The first final delivery fails; the Telegram retry delivers the
        # cached reply without reprocessing the conversation.
        return send_calls >= 2, "", "transport_error" if send_calls < 2 else ""

    monkeypatch.setattr(
        "app.services.telegram_service.handle_free_chat_message",
        fake_conversation,
    )
    monkeypatch.setattr("app.api.routes_telegram.send_message_result", flaky_send)
    update = {"update_id": 900002, **_message("Come va?")}

    first = await client.post("/api/telegram/webhook", json=update, headers=WEBHOOK_HEADERS)
    replay = await client.post("/api/telegram/webhook", json=update, headers=WEBHOOK_HEADERS)

    assert first.status_code == 503
    assert replay.status_code == 200
    assert model_calls == 1
    assert send_calls == 2


async def test_expired_approval_rejected(db_session):
    approval = Approval(
        tool_id="test.tool",
        action="run",
        status="pending",
        expires_at=utcnow() - timedelta(minutes=1),
    )
    db_session.add(approval)
    await db_session.commit()

    reply = await handle_update(db_session, _message(f"/approve {approval.id}"))
    assert isinstance(reply, str)
    assert "expired" in reply.lower()


async def test_replayed_approval_rejected(db_session):
    approval = Approval(
        tool_id="test.tool",
        action="run",
        status="pending",
        expires_at=utcnow() + timedelta(minutes=5),
    )
    db_session.add(approval)
    await db_session.commit()

    first = await handle_update(db_session, _message(f"/approve {approval.id}"))
    assert isinstance(first, str)
    assert "approved" in first.lower()

    replay = await handle_update(db_session, _message(f"/deny {approval.id}"))
    assert isinstance(replay, str)
    assert "already" in replay.lower()


async def test_login_callback_replay_prevented(client, user, capture_adapter, db_session):
    await client.post(
        "/api/auth/login", json={"username": "operator", "password": "correct-horse-battery"}
    )
    nonce = capture_adapter.nonce

    def callback(nonce_value):
        return {
            "callback_query": {
                "from": {"id": "111"},
                "message": {"chat": {"id": "222"}},
                "data": f"login:approve:{nonce_value}",
            }
        }

    first = await handle_update(db_session, callback(nonce))
    await db_session.commit()
    assert isinstance(first, str)
    assert "approved" in first.lower()

    replay = await handle_update(db_session, callback(nonce))
    assert isinstance(replay, str)
    assert "already" in replay.lower() or "unknown" in replay.lower()


async def test_mcp_pairing_approve_command(db_session, monkeypatch):
    captured = {}

    async def fake_send(_request, nonce):
        captured["code"] = nonce
        return "sent"

    monkeypatch.setattr("app.services.mcp_clients._send_pairing_telegram", fake_send)
    pairing = await start_pairing(
        db_session,
        agent_id="codex",
        client_label="Codex remote",
        host_fingerprint="remote-host",
    )
    await db_session.commit()

    reply = await handle_update(db_session, _message(f"/mcpapprove {captured['code']}"))
    assert isinstance(reply, str)
    assert "approved" in reply.lower()
    await db_session.refresh(pairing.request)
    assert pairing.request.status == "approved"


async def test_mcp_token_command_creates_registered_token(db_session):
    reply = await handle_update(db_session, _message("/mcptoken codex Codex remoto"))
    assert isinstance(reply, str)

    assert "MCP token for codex created" in reply
    assert "hmc_" in reply
    token = next(line for line in reply.splitlines() if line.startswith("hmc_"))

    from app.services.mcp_clients import validate_client_token

    client = await validate_client_token(db_session, token=token, agent_id="codex")
    assert client is not None
    assert client.client_label == "Codex remoto"
    assert client.token_hash != token
