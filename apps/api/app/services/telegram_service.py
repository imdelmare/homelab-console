"""Telegram bot: an authenticated client of the control plane.

Authorization is by Telegram user ID and chat ID (never usernames). Every
action is audited. Commands cannot execute arbitrary tools or payloads.
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import utcnow
from app.services.approvals_service import ApprovalError, decide_approval
from app.domain.actors import Actor
from app.services import rate_limit
from app.services.audit import write_audit
from app.services.fixer_dispatch import assign_and_dispatch_fixer
from app.services.auth_service import AuthError, decide_challenge_by_nonce
from app.services.model_providers import (
    MODEL_PROVIDER_IDS,
    get_active_model,
    list_model_profiles,
    switch_active_model,
)
from app.services.mcp_clients import (
    McpClientError,
    create_client_token,
    decide_pairing_by_nonce,
    list_mcp_clients,
)
from app.services.ollama_media import MediaAnalysisError, analyze_audio, analyze_image
from app.services.tasks_service import (
    TaskServiceError,
    get_task,
    list_tasks,
    task_detail,
    task_router_statuses,
)
from app.services.conversation_service import (
    ConversationError,
    confirm_pending_task,
    consume_operations_question,
    create_operations_question,
    handle_free_chat_message,
    handle_conversation_message,
    handle_operations_shortcut,
)
from app.services.watchers import (
    incident_public,
    list_incidents,
    list_watcher_runs,
    resolve_incident_as_handled,
    watcher_status,
)

logger = logging.getLogger("homelab.telegram")
OPERATIONS_SHORTCUTS = {
    "overview": ("lab.summary", "Overview"),
    "network": ("lab.network.summary", "Network"),
    "storage": ("lab.storage.summary", "Storage"),
    "security": ("lab.security.summary", "Security"),
    "automation": ("lab.automation.summary", "Automation"),
    "alerts": ("lab.alerts.recent", "Alerts"),
}
_OPERATIONS_REPLY_PATTERN = re.compile(r"\[OPS:([A-Za-z0-9_-]{8,32})\]")


def _telegram_actor(user_id: str) -> Actor:
    return Actor(kind="telegram", id=user_id, label="telegram operator")


def is_authorized(user_id: str | None, chat_id: str | None) -> bool:
    settings = get_settings()
    if not settings.telegram_allowed_user_id or not settings.telegram_allowed_chat_id:
        return False
    return (
        str(user_id) == str(settings.telegram_allowed_user_id)
        and str(chat_id) == str(settings.telegram_allowed_chat_id)
    )


async def handle_update(
    db: AsyncSession,
    update: dict,
    *,
    on_conversation_start: Callable[[str], Awaitable[None]] | None = None,
) -> str | dict | None:
    """Process a Telegram update. Returns reply text for the chat, or None."""
    callback = update.get("callback_query")
    if callback:
        return await _handle_callback(db, callback)

    message = update.get("message") or {}
    text = str(message.get("text", "") or "").strip()
    caption = str(message.get("caption", "") or "").strip()
    has_media = bool(message.get("photo") or message.get("voice") or message.get("audio"))
    user_id = str((message.get("from") or {}).get("id", ""))
    chat_id = str((message.get("chat") or {}).get("id", ""))

    if not text and not has_media:
        return None

    if not is_authorized(user_id, chat_id):
        await write_audit(
            db, actor=Actor(kind="system", id="telegram-unknown"), source="telegram",
            action="telegram.message", outcome="unauthorized",
            metadata={"user_id": user_id, "chat_id": chat_id},
        )
        return None  # do not reply to strangers

    actor = _telegram_actor(user_id)
    try:
        media = _telegram_media(message)
    except MediaAnalysisError:
        return "This attachment exceeds the allowed limits or is unsupported."
    if has_media and not media:
        return "Attachment analysis is unavailable."
    if media:
        try:
            async with _telegram_typing(chat_id):
                text = await _analyze_telegram_media(media, caption)
        except MediaAnalysisError as exc:
            logger.info("telegram media unavailable: %s", exc.__class__.__name__)
            return "I cannot analyze this attachment right now."
        await write_audit(
            db,
            actor=actor,
            source="telegram",
            action=f"telegram.media.{media['kind']}",
            outcome="success",
            metadata={"bytes": media["file_size"], "duration": media.get("duration", 0)},
        )

    command, _, argument = text.partition(" ")
    argument = argument.strip()

    if command in {"/start", "/menu"}:
        return await _render_home(db, actor)
    if command == "/status":
        return await _render_status(db, actor)
    if command == "/tasks":
        return await _render_tasks(db, actor)
    if command == "/incidents":
        return await _render_incidents(db, actor)
    if command == "/watchers":
        return await _render_watchers(db, actor)
    if command == "/mcp":
        return await _render_mcp(db, actor)
    if command == "/luna":
        return await _render_operations_shortcut(
            db, actor, "overview", chat_id=chat_id
        )
    if command == "/provider":
        return await _cmd_provider(db, actor, argument)
    if command == "/approve":
        return await _cmd_decide_approval(db, actor, argument, approve=True)
    if command == "/deny":
        return await _cmd_decide_approval(db, actor, argument, approve=False)
    if command == "/mcpapprove":
        return await _cmd_decide_mcp_pairing(db, actor, argument, approve=True)
    if command == "/mcpdeny":
        return await _cmd_decide_mcp_pairing(db, actor, argument, approve=False)
    if command == "/mcptoken":
        return await _cmd_create_mcp_token(db, actor, argument)

    if text.startswith("/"):
        return _reply(
            "Unknown command. Use /menu or send me a question.",
            _home_keyboard(),
        )

    if on_conversation_start is not None:
        try:
            await on_conversation_start(chat_id)
        except Exception:
            logger.debug("telegram conversation progress failed", exc_info=True)
    operations_nonce = _operations_reply_nonce(message)
    conversation_id = None
    if operations_nonce:
        try:
            conversation_id = await consume_operations_question(
                db,
                channel="telegram",
                user_ref=user_id,
                nonce=operations_nonce,
                actor=actor,
            )
        except ConversationError:
            return _reply(
                "This Operations question has expired. Open a new one from the panel.",
                _operations_keyboard(),
            )

    try:
        async with _telegram_typing(chat_id):
            if conversation_id:
                result = await handle_conversation_message(
                    db,
                    channel="telegram",
                    user_ref=user_id,
                    content=text,
                    actor=actor,
                    conversation_id=conversation_id,
                )
            else:
                result = await handle_free_chat_message(
                    db,
                    channel="telegram",
                    user_ref=user_id,
                    content=text,
                    actor=actor,
                )
    except ConversationError as exc:
        logger.info("telegram conversation unavailable: %s", exc.__class__.__name__)
        return "Conversation service is not available right now."
    reply_text = _tag_luna_reply(result.assistant_reply, getattr(result, "model", ""))
    keyboard = []
    if conversation_id and result.pending_task_nonce:
        keyboard.append(
            [
                {
                    "text": "Open task",
                    "callback_data": f"conversation:create_task:{result.pending_task_nonce}",
                }
            ]
        )
    keyboard.extend(
        [
            [
                {"text": "🛰 Operations", "callback_data": "nav:operations"},
                {"text": "🏠 Home", "callback_data": "nav:home"},
            ],
        ]
    )
    return _reply(reply_text, keyboard)


def _operations_reply_nonce(message: dict[str, Any]) -> str:
    replied = message.get("reply_to_message")
    if not isinstance(replied, dict):
        return ""
    prompt = str(replied.get("text") or replied.get("caption") or "")
    match = _OPERATIONS_REPLY_PATTERN.search(prompt)
    return match.group(1) if match else ""


def _tag_luna_reply(text: str, model: str) -> str:
    if model != get_settings().conversation_model:
        return text
    return f"{text}\n\n— {model}"


def _telegram_media(message: dict[str, Any]) -> dict[str, Any] | None:
    settings = get_settings()
    if not settings.telegram_media_enabled:
        return None

    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        photo = photos[-1] if isinstance(photos[-1], dict) else {}
        file_id = str(photo.get("file_id") or "")
        file_size = int(photo.get("file_size") or 0)
        if file_id and 0 < file_size <= settings.telegram_media_max_image_bytes:
            return {"kind": "photo", "file_id": file_id, "file_size": file_size}
        if file_id:
            raise MediaAnalysisError("image exceeds configured size limit")

    for kind in ("voice", "audio"):
        item = message.get(kind)
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("file_id") or "")
        file_size = int(item.get("file_size") or 0)
        duration = int(item.get("duration") or 0)
        if not file_id:
            continue
        if file_size <= 0 or file_size > settings.telegram_media_max_audio_bytes:
            raise MediaAnalysisError("audio exceeds configured size limit")
        if duration <= 0 or duration > settings.telegram_media_max_audio_seconds:
            raise MediaAnalysisError("audio exceeds configured duration limit")
        return {
            "kind": kind,
            "file_id": file_id,
            "file_size": file_size,
            "duration": duration,
        }
    return None


async def _analyze_telegram_media(media: dict[str, Any], caption: str) -> str:
    data = await _download_telegram_file(str(media["file_id"]), int(media["file_size"]))
    if media["kind"] == "photo":
        prompt = caption or "Descrivi brevemente questa immagine in italiano."
        analysis = await analyze_image(data, prompt)
        return (
            f"Messaggio dell'operatore con foto: {caption or 'descrivi la foto'}\n"
            f"Analisi locale della foto:\n{analysis}"
        )

    prompt = (
        "Trascrivi accuratamente questo messaggio vocale. "
        "Restituisci solo le parole pronunciate."
    )
    transcript = await analyze_audio(data, prompt)
    prefix = f"Nota dell'operatore: {caption}\n" if caption else ""
    return f"{prefix}Messaggio vocale trascritto localmente:\n{transcript}"


async def _download_telegram_file(file_id: str, expected_size: int) -> bytes:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise MediaAnalysisError("Telegram bot is not configured")
    api_base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            metadata_response = await client.post(
                f"{api_base}/getFile",
                json={"file_id": file_id},
            )
            metadata_response.raise_for_status()
            metadata = metadata_response.json()
            result = metadata.get("result") if isinstance(metadata, dict) else None
            file_path = result.get("file_path") if isinstance(result, dict) else None
            if (
                not isinstance(file_path, str)
                or not file_path
                or file_path.startswith("/")
                or ".." in file_path.split("/")
            ):
                raise MediaAnalysisError("Telegram returned an invalid file path")
            file_response = await client.get(
                f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
            )
            file_response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise MediaAnalysisError("Telegram media download failed") from exc

    data = file_response.content
    if not data or len(data) > expected_size + 1024:
        raise MediaAnalysisError("Telegram media size mismatch")
    return data


async def _handle_callback(db: AsyncSession, callback: dict) -> str | dict | None:
    user_id = str((callback.get("from") or {}).get("id", ""))
    chat_id = str(
        (((callback.get("message") or {}).get("chat")) or {}).get("id", "")
    )
    data = str(callback.get("data", "") or "")

    if not rate_limit.check("telegram.callback", user_id or "unknown"):
        return None

    if not is_authorized(user_id, chat_id):
        await write_audit(
            db, actor=Actor(kind="system", id="telegram-unknown"), source="telegram",
            action="telegram.callback", outcome="unauthorized",
            metadata={"user_id": user_id, "chat_id": chat_id},
        )
        return None

    actor = _telegram_actor(user_id)

    if data == "nav:home":
        return await _render_home(db, actor)
    if data == "nav:more":
        return await _render_more(db, actor)
    if data == "nav:status":
        return await _render_status(db, actor)
    if data == "nav:incidents":
        return await _render_incidents(db, actor)
    if data == "nav:tasks":
        return await _render_tasks(db, actor)
    if data == "nav:watchers":
        return await _render_watchers(db, actor)
    if data == "nav:mcp":
        return await _render_mcp(db, actor)
    if data == "nav:operations":
        return await _render_operations(db, actor)
    if data == "operations:ask":
        nonce = await create_operations_question(
            db,
            channel="telegram",
            user_ref=user_id,
            actor=actor,
        )
        return {
            "text": (
                "What would you like me to check using live Homelab data?\n\n"
                f"[OPS:{nonce}]"
            ),
            "reply_markup": {
                "force_reply": True,
                "selective": True,
                "input_field_placeholder": "Ask one live Operations question",
            },
            "send_new": True,
        }
    if data.startswith("operations:summary:"):
        shortcut_id = data.rsplit(":", 1)[-1]
        shortcut = OPERATIONS_SHORTCUTS.get(shortcut_id)
        if shortcut is None:
            return _reply("Unknown Operations shortcut.", _operations_keyboard())
        tool_id, label = shortcut
        try:
            async with _telegram_typing(chat_id):
                result = await handle_operations_shortcut(
                    db,
                    channel="telegram",
                    user_ref=user_id,
                    tool_id=tool_id,
                    label=label,
                    actor=actor,
                )
        except ConversationError:
            return _reply(
                "Operations data is not available right now.",
                _operations_keyboard(),
            )
        return _reply(
            _tag_luna_reply(result.assistant_reply, result.model),
            _operations_keyboard(),
        )
    if data == "luna:summary":
        return await _render_operations_shortcut(
            db, actor, "overview", chat_id=chat_id
        )
    if data == "luna:triage":
        return await _render_luna(db, actor, "triage", chat_id=chat_id)
    if data.startswith("approval:approve:") or data.startswith("approval:deny:"):
        approve = data.split(":", 2)[1] == "approve"
        text = await _cmd_decide_approval(db, actor, data.rsplit(":", 1)[-1], approve)
        return _reply(text, _back_keyboard("nav:home"))
    if data.startswith("task:detail:"):
        return await _render_task_detail(db, actor, data.rsplit(":", 1)[-1])
    if data.startswith("task:assign_fixer:"):
        task_id = data.rsplit(":", 1)[-1]
        try:
            task, dispatch = await assign_and_dispatch_fixer(
                db,
                task_id,
                actor,
                source="telegram",
            )
        except TaskServiceError as exc:
            return _reply(
                f"Fixer assignment failed: {exc.message}",
                _back_keyboard("nav:tasks"),
            )
        text = (
            f"🛠 Fixer started: {task.title}"
            if dispatch.ok
            else f"🛠 Task assigned, startup failed ({dispatch.code}): {dispatch.message}"
        )
        return _reply(
            text,
            [[{"text": "Open task", "callback_data": f"task:detail:{task.id}"}], *_back_keyboard("nav:tasks")],
        )
    if data.startswith("incident:detail:"):
        return await _render_incident_detail(db, actor, data.rsplit(":", 1)[-1])
    if data.startswith("incident:handled:"):
        incident_id = data.rsplit(":", 1)[-1]
        try:
            incident = await resolve_incident_as_handled(
                db,
                incident_id=incident_id,
                actor=actor,
                note="Resolved from Telegram quick action.",
            )
        except Exception as exc:
            return _reply(f"Incident resolve failed: {exc}", _back_keyboard("nav:incidents"))
        return _reply(
            f"Incident marked handled: {incident.title}",
            _back_keyboard("nav:incidents"),
        )

    if data.startswith("login:approve:") or data.startswith("login:deny:"):
        approve = data.startswith("login:approve:")
        nonce = data.rsplit(":", 1)[-1]
        try:
            await decide_challenge_by_nonce(db, nonce, approve, actor)
        except AuthError as exc:
            return f"Login challenge: {exc.message}"
        return "Login approved. Complete it in the browser." if approve else "Login rejected."

    if data.startswith("mcp_pairing:approve:") or data.startswith("mcp_pairing:deny:"):
        approve = data.startswith("mcp_pairing:approve:")
        nonce = data.rsplit(":", 1)[-1]
        try:
            request = await decide_pairing_by_nonce(db, nonce=nonce, approve=approve, actor=actor)
        except McpClientError as exc:
            return f"MCP pairing: {exc.message}"
        if request.status == "expired":
            return "MCP pairing expired."
        return "MCP client approved. Complete pairing in the MCP process." if approve else "MCP client denied."

    if data.startswith("conversation:create_task:"):
        nonce = data.rsplit(":", 1)[-1]
        try:
            task = await confirm_pending_task(db, nonce=nonce, actor=actor, channel="telegram")
        except ConversationError as exc:
            return f"Task proposal: {exc}"
        return f"Task created: {task.title} id={task.id[:8]}"

    await write_audit(
        db, actor=actor, source="telegram", action="telegram.callback",
        outcome="unknown_action", metadata={"data_prefix": data.split(":", 1)[0]},
    )
    return None


async def _cmd_status(db: AsyncSession, actor: Actor) -> str:
    from app.providers.registry import provider_health_snapshot

    snapshots = await provider_health_snapshot(db)
    active = await get_active_model(db)
    lines = [f"Model: {active.id if active else 'unset'}"]
    for health in snapshots:
        lines.append(f"{health.provider_id}: {health.status}")
    await write_audit(db, actor=actor, source="telegram", action="telegram.status", outcome="success")
    return "\n".join(lines)


async def _cmd_tasks(db: AsyncSession, actor: Actor) -> str:
    tasks = await list_tasks(db, limit=10)
    await write_audit(db, actor=actor, source="telegram", action="telegram.tasks", outcome="success")
    if not tasks:
        return "No tasks."
    return "\n".join(f"[{task.status}] {task.title} id={task.id[:8]}" for task in tasks)


async def _render_home(db: AsyncSession, actor: Actor) -> dict:
    from app.providers.registry import provider_health_snapshot

    snapshots = await provider_health_snapshot(db)
    open_incidents = await list_incidents(db, status="open", limit=100)
    tasks = await list_tasks(db, status="open", limit=100)
    watcher = await watcher_status(db)
    clients = await list_mcp_clients(db)
    degraded = sum(1 for item in snapshots if item.status not in {"healthy", "unknown"})
    online_clients = sum(1 for client in clients if _is_recent(client.last_seen_at))
    health = (
        f"🔴 {len(open_incidents)} open incident(s)"
        if open_incidents
        else (
            "🟢 Systems nominal"
            if degraded == 0
            else f"🟠 {degraded} provider(s) need attention"
        )
    )
    automation = "on" if watcher.get("enabled") else "off"
    lines = [
        "🏠 Homelab",
        health,
        "",
        f"🚨 {len(open_incidents)} incidents  ·  📋 {len(tasks)} tasks",
        f"⚡ Automation {automation}  ·  🤖 {online_clients}/{len(clients)} agents online",
        "",
        "Send a message for tool-free chat, or open Operations for live data.",
    ]
    await write_audit(db, actor=actor, source="telegram", action="telegram.home", outcome="success")
    return _reply("\n".join(lines), _home_keyboard())


async def _render_more(db: AsyncSession, actor: Actor) -> dict:
    await write_audit(db, actor=actor, source="telegram", action="telegram.more", outcome="success")
    return _reply(
        "••• More\n\nAutomation, connected agents and the full operational status.",
        _more_keyboard(),
    )


async def _render_operations(db: AsyncSession, actor: Actor) -> dict:
    await write_audit(
        db,
        actor=actor,
        source="telegram",
        action="telegram.operations",
        outcome="success",
    )
    return _reply(
        (
            "🛰 Operations\n\n"
            "Choose a live summary or ask one governed question. "
            "Normal messages outside this panel remain tool-free chat."
        ),
        _operations_keyboard(),
    )


async def _render_operations_shortcut(
    db: AsyncSession,
    actor: Actor,
    shortcut_id: str,
    *,
    chat_id: str,
) -> dict:
    shortcut = OPERATIONS_SHORTCUTS[shortcut_id]
    try:
        async with _telegram_typing(chat_id):
            result = await handle_operations_shortcut(
                db,
                channel="telegram",
                user_ref=actor.id,
                tool_id=shortcut[0],
                label=shortcut[1],
                actor=actor,
            )
    except ConversationError:
        return _reply(
            "Operations data is not available right now.",
            _operations_keyboard(),
        )
    return _reply(
        _tag_luna_reply(result.assistant_reply, result.model),
        _operations_keyboard(),
    )


async def _render_status(db: AsyncSession, actor: Actor) -> dict:
    from app.providers.registry import provider_health_snapshot

    snapshots = await provider_health_snapshot(db)
    open_incidents = await list_incidents(db, status="open", limit=100)
    tasks = await list_tasks(db, status="open", limit=100)
    watcher = await watcher_status(db)
    clients = await list_mcp_clients(db)
    router_statuses = await task_router_statuses(db, [task.id for task in tasks])
    degraded = sum(1 for item in snapshots if item.status not in {"healthy", "unknown"})
    online_clients = sum(1 for client in clients if _is_recent(client.last_seen_at))
    routed = sum(1 for status in router_statuses.values() if status == "routed")
    active_routing = sum(1 for status in router_statuses.values() if status in {"queued", "running"})
    failed = sum(1 for status in router_statuses.values() if status in {"failed", "policy_failed"})
    last_run = await list_watcher_runs(db, limit=1)
    lines = [
        "📊 Full status",
        "",
        f"Providers: {len(snapshots) - degraded} ok / {degraded} degraded",
        f"Open incidents: {len(open_incidents)}",
        f"Open tasks: {len(tasks)}",
        f"Watcher: {'enabled' if watcher.get('enabled') else 'disabled'} / {watcher.get('interval_seconds')}s",
        f"Last run: {_time_ago(last_run[0].started_at) if last_run else 'never'}",
        f"MCP: {online_clients}/{len(clients)} online",
        f"Router: {routed} routed / {active_routing} active / {failed} failed",
    ]
    await write_audit(db, actor=actor, source="telegram", action="telegram.status", outcome="success")
    return _reply("\n".join(lines), _back_keyboard("nav:more"))


async def _render_incidents(db: AsyncSession, actor: Actor) -> dict:
    incidents = await list_incidents(db, status="open", limit=100)
    if not incidents:
        text = "No open incidents."
        keyboard = [[{"text": "🏠 Home", "callback_data": "nav:home"}]]
    else:
        visible = incidents[:3]
        lines = [f"🚨 Open incidents · {len(incidents)}", ""]
        keyboard_rows: list[list[dict[str, str]]] = []
        for index, incident in enumerate(visible, 1):
            lines.append(
                f"{index}. {incident.severity} / {incident.provider_id}\n"
                f"{_short(incident.title, 92)}\n"
                f"Task: {incident.task_id[:8] if incident.task_id else 'none'}"
            )
            keyboard_rows.append(
                [{"text": f"{index}. Details", "callback_data": f"incident:detail:{incident.id}"}]
            )
        if len(incidents) > len(visible):
            lines.extend(
                [
                    "",
                    f"Showing {len(visible)} of {len(incidents)}. "
                    "Open the web console for the full list.",
                ]
            )
        keyboard_rows.append([{"text": "🏠 Home", "callback_data": "nav:home"}])
        text = "\n\n".join(lines)
        keyboard = keyboard_rows
    await write_audit(db, actor=actor, source="telegram", action="telegram.incidents", outcome="success")
    return _reply(text, keyboard)


async def _render_incident_detail(db: AsyncSession, actor: Actor, incident_id: str) -> dict:
    incident = next((item for item in await list_incidents(db, status=None, limit=100) if item.id == incident_id), None)
    if incident is None:
        return _reply("Incident not found.", _back_keyboard("nav:incidents"))
    data = incident_public(incident)
    lines = [
        f"Incident {incident.id[:8]}",
        "",
        f"Status: {data['status']} / {data['severity']}",
        f"Provider: {data['provider_id']}",
        f"Watcher: {data['watcher_id']}",
        f"Task: {data['task_id'][:8] if data['task_id'] else 'none'}",
        f"Seen: {data['occurrences']} occurrence(s)",
        "",
        _short(data["title"], 240),
        _short(data["description"], 600),
    ]
    keyboard = [
        [{"text": "Task", "callback_data": f"task:detail:{incident.task_id}"}] if incident.task_id else [],
        [{"text": "Mark handled", "callback_data": f"incident:handled:{incident.id}"}],
        *_back_keyboard("nav:incidents"),
    ]
    return _reply("\n".join(line for line in lines if line), [row for row in keyboard if row])


async def _render_tasks(db: AsyncSession, actor: Actor) -> dict:
    tasks = await list_tasks(db, status="open", limit=100)
    router_statuses = await task_router_statuses(db, [task.id for task in tasks])
    if not tasks:
        return _reply("No open tasks.", [[{"text": "🏠 Home", "callback_data": "nav:home"}]])
    visible = tasks[:3]
    lines = [f"📋 Open tasks · {len(tasks)}", ""]
    keyboard: list[list[dict[str, str]]] = []
    for index, task in enumerate(visible, 1):
        router = router_statuses.get(task.id) or "unrouted"
        lines.append(
            f"{index}. {task.status} / {task.source} / {router}\n"
            f"{_short(task.title, 110)}\n"
            f"Owner: {task.assigned_agent or 'unassigned'}"
        )
        keyboard.append([{"text": f"{index}. Details", "callback_data": f"task:detail:{task.id}"}])
    if len(tasks) > len(visible):
        lines.extend(
            [
                "",
                f"Showing {len(visible)} of {len(tasks)}. "
                "Open the web console for the full list.",
            ]
        )
    keyboard.append([{"text": "🏠 Home", "callback_data": "nav:home"}])
    await write_audit(db, actor=actor, source="telegram", action="telegram.tasks", outcome="success")
    return _reply("\n\n".join(lines), keyboard)


async def _render_task_detail(db: AsyncSession, actor: Actor, task_id: str) -> dict:
    task = await get_task(db, task_id)
    if task is None:
        return _reply("Task not found.", _back_keyboard("nav:tasks"))
    detail = await task_detail(db, task)
    router_events = [event for event in detail["events"] if event["kind"] in {"task.router_decision", "task.router_failed"}]
    router = "unrouted"
    if router_events:
        router = "failed" if router_events[-1]["kind"] == "task.router_failed" else "routed"
    findings = detail["findings"][:3]
    lines = [
        f"Task {task.id[:8]}",
        "",
        f"Status: {task.status}",
        f"Source: {task.source}",
        f"Owner: {task.assigned_agent or 'unassigned'}",
        f"Router: {router}",
        "",
        _short(task.title, 220),
        _short(task.goal, 700),
    ]
    if findings:
        lines.append("")
        lines.append("Findings:")
        lines.extend(f"- {item['severity']}: {_short(item['title'], 120)}" for item in findings)
    keyboard = [
        *(
            [[{"text": "🛠 Assign to Fixer", "callback_data": f"task:assign_fixer:{task.id}"}]]
            if task.status == "open" and not task.assigned_agent
            else []
        ),
        [{"text": "Luna summary", "callback_data": "luna:summary"}],
        *_back_keyboard("nav:tasks"),
    ]
    return _reply("\n".join(lines), keyboard)


async def _render_watchers(db: AsyncSession, actor: Actor) -> dict:
    status = await watcher_status(db)
    runs = await list_watcher_runs(db, limit=5)
    lines = [
        "Watcher status",
        "",
        f"Automation: {'enabled' if status.get('enabled') else 'disabled'}",
        f"Interval: {status.get('interval_seconds')}s",
        f"Min severity: {status.get('min_severity')}",
        "",
        "Recent runs:",
    ]
    if not runs:
        lines.append("- none")
    for run in runs:
        lines.append(
            f"- {run.watcher_id}: {run.status} / +{run.created_tasks} upd {run.updated_incidents} res {run.resolved_incidents}"
        )
    await write_audit(db, actor=actor, source="telegram", action="telegram.watchers", outcome="success")
    return _reply("\n".join(lines), _back_keyboard("nav:more"))


async def _render_mcp(db: AsyncSession, actor: Actor) -> dict:
    clients = await list_mcp_clients(db)
    lines = ["MCP clients", ""]
    if not clients:
        lines.append("No MCP clients.")
    for client in clients:
        state = "revoked" if client.revoked_at else "online" if _is_recent(client.last_seen_at) else "idle"
        lines.append(f"- {client.agent_id}: {state} / {client.client_label or client.token_hint}")
    await write_audit(db, actor=actor, source="telegram", action="telegram.mcp", outcome="success")
    return _reply("\n".join(lines), _back_keyboard("nav:more"))


async def _render_luna(db: AsyncSession, actor: Actor, mode: str, *, chat_id: str = "") -> dict:
    prompt = (
        "Riassumi lo stato operativo Homelab Console in modo breve. "
        "Non creare task, non chiudere task, non eseguire azioni. "
        "Usa solo dati osservabili e indica cosa monitorare."
    )
    if mode == "triage":
        prompt = (
            "Fai triage delle task e incidenti aperti. "
            "Non creare task, non chiudere task, non eseguire azioni. "
            "Dammi priorita, motivo e prossimo controllo read-only."
    )
    try:
        async with _telegram_typing(chat_id):
            result = await handle_conversation_message(
                db,
                channel="telegram",
                user_ref=actor.id,
                content=prompt,
                actor=actor,
            )
    except ConversationError as exc:
        logger.info("telegram luna unavailable: %s", exc.__class__.__name__)
        return _reply("Luna is not available right now.", _home_keyboard())
    text = _tag_luna_reply(result.assistant_reply, getattr(result, "model", ""))
    if result.created_task_id or result.pending_task_nonce:
        text += "\n\nNote: Luna proposed a task, but this panel is monitoring-only."
    await write_audit(db, actor=actor, source="telegram", action=f"telegram.luna.{mode}", outcome="success")
    return _reply(
        text,
        [
            [
                {"text": "✨ Summary", "callback_data": "luna:summary"},
                {"text": "🧭 Triage", "callback_data": "luna:triage"},
            ],
            *_nav_rows(),
        ],
    )


async def _cmd_provider(db: AsyncSession, actor: Actor, argument: str) -> str:
    if not argument:
        profiles = await list_model_profiles(db)
        return "\n".join(
            f"{'* ' if profile.active else '  '}{profile.id} ({profile.status})"
            for profile in profiles
        ) or "No model profiles."

    provider_id = argument.lower()
    if provider_id not in MODEL_PROVIDER_IDS:
        await write_audit(
            db, actor=actor, source="telegram", action="model.switch",
            outcome="invalid_provider", metadata={"requested": provider_id},
        )
        return f"Unknown model provider: {provider_id}. Use one of: {', '.join(MODEL_PROVIDER_IDS)}"

    await switch_active_model(db, provider_id, actor, source="telegram")
    return f"Active model provider is now {provider_id}."


async def _cmd_decide_approval(
    db: AsyncSession, actor: Actor, approval_id: str, approve: bool
) -> str:
    if not approval_id:
        return "Usage: /approve <approval_id> or /deny <approval_id>"

    try:
        approval, outcome = await decide_approval(
            db, approval_id=approval_id, approve=approve, actor=actor, source="telegram"
        )
    except ApprovalError:
        return "Unknown approval id."
    if outcome == "replayed":
        return f"Approval already {approval.status}."
    if outcome == "expired":
        return "Approval expired."
    return f"Approval {approval.status}."


async def _cmd_decide_mcp_pairing(
    db: AsyncSession, actor: Actor, code: str, approve: bool
) -> str:
    if not code:
        return "Usage: /mcpapprove <code> or /mcpdeny <code>"
    try:
        request = await decide_pairing_by_nonce(db, nonce=code.strip(), approve=approve, actor=actor)
    except McpClientError as exc:
        return f"MCP pairing: {exc.message}"
    if request.status == "expired":
        return "MCP pairing expired."
    return "MCP client approved. Complete pairing in the MCP process." if approve else "MCP client denied."


async def _cmd_create_mcp_token(db: AsyncSession, actor: Actor, argument: str) -> str:
    agent_id, _, label = argument.partition(" ")
    if not agent_id:
        return "Usage: /mcptoken <codex|claude|fixer|cline|opencode> [label]"
    try:
        result = await create_client_token(
            db,
            agent_id=agent_id,
            client_label=label.strip() or f"{agent_id.strip().lower()} remote",
            host_fingerprint="telegram-issued",
            actor=actor,
            source="telegram",
        )
    except McpClientError as exc:
        return f"MCP token: {exc.message}"
    return (
        f"MCP token for {result.client.agent_id} created. Paste this as Bearer token in the MCP client:\n"
        f"{result.token}\n\n"
        f"Hint: {result.client.token_hint}. Revoke it from MCP clients if needed."
    )


def _reply(text: str, keyboard: list[list[dict[str, str]]] | None = None) -> dict:
    payload: dict[str, Any] = {"text": _short(text, 3900)}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return payload


def _home_keyboard() -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "🚨 Incidents", "callback_data": "nav:incidents"},
            {"text": "📋 Tasks", "callback_data": "nav:tasks"},
        ],
        [
            {"text": "🛰 Operations", "callback_data": "nav:operations"},
            {"text": "••• More", "callback_data": "nav:more"},
        ],
    ]


def _operations_keyboard() -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "📊 Overview", "callback_data": "operations:summary:overview"},
            {"text": "🌐 Network", "callback_data": "operations:summary:network"},
        ],
        [
            {"text": "💾 Storage", "callback_data": "operations:summary:storage"},
            {"text": "🔒 Security", "callback_data": "operations:summary:security"},
        ],
        [
            {"text": "⚡ Automation", "callback_data": "operations:summary:automation"},
            {"text": "🚨 Alerts", "callback_data": "operations:summary:alerts"},
        ],
        [
            {"text": "🧭 Triage", "callback_data": "luna:triage"},
            {"text": "❓ Ask live question", "callback_data": "operations:ask"},
        ],
        [{"text": "🏠 Home", "callback_data": "nav:home"}],
    ]


def _more_keyboard() -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "⚡ Automation", "callback_data": "nav:watchers"},
            {"text": "🤖 Agents", "callback_data": "nav:mcp"},
        ],
        [
            {"text": "📊 Full status", "callback_data": "nav:status"},
            {"text": "🏠 Home", "callback_data": "nav:home"},
        ],
    ]


def _nav_rows() -> list[list[dict[str, str]]]:
    return [[{"text": "🏠 Home", "callback_data": "nav:home"}]]


def _back_keyboard(target: str) -> list[list[dict[str, str]]]:
    return [[{"text": "‹ Back", "callback_data": target}, {"text": "🏠 Home", "callback_data": "nav:home"}]]


def _short(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _is_recent(value: datetime | None, *, within: timedelta = timedelta(minutes=3)) -> bool:
    if value is None:
        return False
    return utcnow() - _aware_utc(value) <= within


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _time_ago(value: datetime | None) -> str:
    if value is None:
        return "never"
    seconds = max(0, int((utcnow() - _aware_utc(value)).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    return f"{minutes // 60}h ago"


async def send_message_result(
    chat_id: str, text: str, reply_markup: dict | None = None
) -> tuple[bool, str, str]:
    settings = get_settings()
    if not settings.telegram_bot_token:
        return False, "", "not_configured"
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                return False, "", f"http_{response.status_code}"
            body = response.json()
            return True, str(body.get("result", {}).get("message_id", "")), ""
    except httpx.HTTPError:
        logger.warning("telegram send_message failed")
        return False, "", "transport_error"


async def send_message(chat_id: str, text: str, reply_markup: dict | None = None) -> bool:
    ok, _, _ = await send_message_result(chat_id, text, reply_markup)
    return ok


async def answer_callback_query_result(callback_query_id: str) -> bool:
    settings = get_settings()
    if not settings.telegram_bot_token or not callback_query_id:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/answerCallbackQuery"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(url, json={"callback_query_id": callback_query_id})
            return response.status_code == 200
    except httpx.HTTPError:
        logger.debug("telegram answer_callback_query failed")
        return False


@asynccontextmanager
async def _telegram_typing(chat_id: str) -> AsyncIterator[None]:
    settings = get_settings()
    if not chat_id or not settings.telegram_bot_token:
        yield
        return

    task = asyncio.create_task(_typing_loop(chat_id))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _typing_loop(chat_id: str) -> None:
    while True:
        await _send_chat_action(chat_id, "typing")
        await asyncio.sleep(4)


async def _send_chat_action(chat_id: str, action: str) -> None:
    settings = get_settings()
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendChatAction"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"chat_id": chat_id, "action": action})
    except httpx.HTTPError:
        logger.debug("telegram chat action failed")


async def edit_message_result(
    chat_id: str,
    message_id: str,
    text: str,
    reply_markup: dict | None = None,
) -> tuple[bool, str]:
    settings = get_settings()
    if not settings.telegram_bot_token or not message_id:
        return False, "not_configured"
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/editMessageText"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if body.get("description") == "Bad Request: message is not modified":
                    return True, ""
                return False, f"http_{response.status_code}"
            return True, ""
    except httpx.HTTPError:
        logger.warning("telegram edit_message failed")
        return False, "transport_error"
