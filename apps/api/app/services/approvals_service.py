"""Approval request lifecycle for write and high-risk tool invocations.

Creation, decision, listing and expiry live here; consumption stays the
execution core's atomic conditional UPDATE. Telegram and REST both route
decisions through decide_approval so audit and replay behavior are
identical on every surface.
"""

import hashlib
import logging
from datetime import timedelta

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import Approval, utcnow
from app.domain.actors import Actor
from app.services.audit import write_audit
from app.services.inventory import provider_config
from app.services.redaction import redact
from app.tools.registry import get_tool

logger = logging.getLogger("homelab.approvals")


class ApprovalError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def input_digest(validated: BaseModel) -> str:
    """Canonical SHA-256 of a validated tool input.

    The execution core computes the same digest from its own validated
    model, so an approval only unlocks the exact input it was requested
    for — never the same tool with different arguments.
    """
    return hashlib.sha256(validated.model_dump_json().encode()).hexdigest()


def approval_public(approval: Approval) -> dict:
    return {
        "id": approval.id,
        "tool_id": approval.tool_id,
        "action": approval.action,
        "status": approval.status,
        "task_id": approval.task_id,
        "requested_by": approval.requested_by,
        "decided_by": approval.decided_by,
        "created_at": approval.created_at,
        "expires_at": approval.expires_at,
        "decided_at": approval.decided_at,
        "consumed_at": approval.consumed_at,
    }


def _action_summary(tool_id: str, safe_input: dict) -> str:
    rendered = " ".join(f"{key}={value}" for key, value in sorted(safe_input.items()))
    summary = f"{tool_id} {rendered}".strip()
    return summary[:128]


def _approval_warning(tool_id: str, safe_input: dict) -> str:
    if tool_id != "proxmox.lxc.shutdown":
        return ""
    vmid = safe_input.get("vmid")
    critical = provider_config("proxmox").get("critical_lxc_vmids", []) or []
    if isinstance(vmid, int) and not isinstance(vmid, bool) and vmid in critical:
        return (
            "⚠️ CRITICAL TARGET: shutdown may interrupt services or connectivity. "
            "Verify the recovery path before approving."
        )
    return ""


async def request_approval(
    db: AsyncSession,
    *,
    tool_id: str,
    raw_input: dict | None,
    actor: Actor,
    task_id: str | None = None,
    source: str = "rest",
) -> Approval:
    tool = get_tool(tool_id)
    if tool is None or not tool.enabled:
        raise ApprovalError("unknown_tool", f"unknown or disabled tool: {tool_id}")
    if tool.mode != "write" and tool.risk != "high":
        raise ApprovalError("not_approvable", "tool does not require an approval")
    try:
        validated = tool.input_model.model_validate(raw_input or {})
    except ValidationError:
        raise ApprovalError("invalid_input", "input does not match the tool schema") from None

    settings = get_settings()
    safe_input = redact(raw_input or {}, provider_id=tool.provider_id or None)
    approval = Approval(
        task_id=task_id,
        tool_id=tool_id,
        action=_action_summary(tool_id, safe_input),
        input_hash=input_digest(validated),
        requested_by=actor.audit_id(),
        expires_at=utcnow() + timedelta(seconds=settings.approval_ttl_seconds),
    )
    db.add(approval)
    await db.flush()
    await write_audit(
        db,
        actor=actor,
        source=source,
        action="approval.request",
        outcome="pending",
        tool_id=tool_id,
        task_id=task_id or "",
        metadata={"approval_id": approval.id, "input": safe_input},
    )
    delivered = await _notify_operator(approval, safe_input)
    await write_audit(
        db,
        actor=actor,
        source=source,
        action="approval.notify",
        outcome="sent" if delivered else "not_delivered",
        tool_id=tool_id,
        metadata={"approval_id": approval.id},
    )
    return approval


async def _notify_operator(approval: Approval, safe_input: dict) -> bool:
    # Lazy import: telegram_service imports this module for decisions.
    from app.services.telegram_service import send_message

    settings = get_settings()
    chat_id = settings.telegram_allowed_chat_id
    if not chat_id or not settings.telegram_bot_token:
        return False
    minutes = max(1, int((settings.approval_ttl_seconds + 59) // 60))
    lines = [
        "🔐 Write approval request",
        f"Tool: {approval.tool_id}",
        f"From: {approval.requested_by}",
    ]
    if approval.task_id:
        lines.append(f"Task: {approval.task_id}")
    if safe_input:
        rendered = " ".join(f"{key}={value}" for key, value in sorted(safe_input.items()))
        lines.append(f"Input: {rendered[:300]}")
    warning = _approval_warning(approval.tool_id, safe_input)
    if warning:
        lines.append(warning)
    lines.append(f"Expires in {minutes} min. ID: {approval.id}")
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"approval:approve:{approval.id}"},
                {"text": "⛔ Deny", "callback_data": f"approval:deny:{approval.id}"},
            ]
        ]
    }
    delivered = await send_message(chat_id, "\n".join(lines), keyboard)
    if not delivered:
        logger.warning("approval request %s not delivered to telegram", approval.id)
    return delivered


async def decide_approval(
    db: AsyncSession,
    *,
    approval_id: str,
    approve: bool,
    actor: Actor,
    source: str = "telegram",
) -> tuple[Approval, str]:
    """Decide a pending approval. Returns (approval, outcome) where outcome
    is one of approved/denied/expired/replayed; raises for an unknown id."""
    result = await db.execute(select(Approval).where(Approval.id == approval_id))
    approval = result.scalar_one_or_none()
    if approval is None:
        await write_audit(
            db, actor=actor, source=source, action="approval.decide",
            outcome="unknown", metadata={"approval_id": approval_id},
        )
        raise ApprovalError("unknown_approval", "unknown approval id")

    from app.services.auth_service import _aware  # shared naive-datetime handling

    if approval.status != "pending":
        await write_audit(
            db, actor=actor, source=source, action="approval.decide",
            outcome="replayed", metadata={"approval_id": approval_id},
        )
        return approval, "replayed"

    if (expires_at := _aware(approval.expires_at)) is None or expires_at < utcnow():
        approval.status = "expired"
        await write_audit(
            db, actor=actor, source=source, action="approval.decide",
            outcome="expired", metadata={"approval_id": approval_id},
        )
        return approval, "expired"

    approval.status = "approved" if approve else "denied"
    approval.decided_by = actor.audit_id()
    approval.decided_at = utcnow()
    await write_audit(
        db, actor=actor, source=source, action="approval.decide",
        outcome=approval.status, metadata={"approval_id": approval_id},
    )
    return approval, approval.status


async def get_approval(db: AsyncSession, approval_id: str) -> Approval | None:
    return await db.get(Approval, approval_id)


async def list_approvals(
    db: AsyncSession, *, status: str | None = None, limit: int = 50
) -> list[Approval]:
    query = select(Approval).order_by(Approval.created_at.desc()).limit(limit)
    if status:
        query = query.where(Approval.status == status)
    result = await db.execute(query)
    return list(result.scalars())
