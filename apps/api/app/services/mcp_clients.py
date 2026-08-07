from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.models import McpClient, McpPairingRequest, utcnow
from app.domain.actors import Actor
from app.services.audit import write_audit

VALID_MCP_AGENT_IDS = {"claude", "fixer", "codex", "cline", "opencode"}
PAIRING_TTL_SECONDS = 300


class McpClientError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class PairingStart:
    request: McpPairingRequest
    pairing_secret: str


@dataclass(frozen=True)
class PairingConsumeResult:
    client: McpClient
    token: str


def _hash(value: str) -> str:
    key = get_settings().session_secret.encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _clean_agent_id(agent_id: str) -> str:
    value = agent_id.strip().lower()
    if value not in VALID_MCP_AGENT_IDS:
        raise McpClientError(
            "invalid_agent", "MCP agent must be one of: claude, fixer, codex, cline, opencode"
        )
    return value


def _token_hint(token: str) -> str:
    return token[-8:]


def _pairing_code() -> str:
    length = max(6, min(12, int(get_settings().otp_length)))
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def _aware(value):
    if value.tzinfo is None:
        from datetime import UTC

        return value.replace(tzinfo=UTC)
    return value


def mcp_client_public(client: McpClient) -> dict:
    return {
        "id": client.id,
        "agent_id": client.agent_id,
        "client_label": client.client_label,
        "host_fingerprint": client.host_fingerprint,
        "token_hint": client.token_hint,
        "created_at": client.created_at,
        "approved_at": client.approved_at,
        "last_seen_at": client.last_seen_at,
        "revoked_at": client.revoked_at,
        "revoked_reason": client.revoked_reason,
        "created_by": client.created_by,
    }


def mcp_pairing_public(request: McpPairingRequest) -> dict:
    return {
        "id": request.id,
        "agent_id": request.agent_id,
        "client_label": request.client_label,
        "host_fingerprint": request.host_fingerprint,
        "status": request.status,
        "created_at": request.created_at,
        "expires_at": request.expires_at,
        "approved_at": request.approved_at,
        "denied_at": request.denied_at,
        "consumed_at": request.consumed_at,
        "decided_by": request.decided_by,
        "delivery_status": request.delivery_status,
    }


async def start_pairing(
    db: AsyncSession,
    *,
    agent_id: str,
    client_label: str,
    host_fingerprint: str,
) -> PairingStart:
    agent_id = _clean_agent_id(agent_id)
    pairing_secret = secrets.token_urlsafe(32)
    approve_nonce = _pairing_code()
    request = McpPairingRequest(
        agent_id=agent_id,
        client_label=client_label[:128],
        host_fingerprint=host_fingerprint[:128],
        pairing_secret_hash=_hash(pairing_secret),
        approve_nonce_hash=_hash(approve_nonce),
        expires_at=utcnow() + timedelta(seconds=PAIRING_TTL_SECONDS),
    )
    db.add(request)
    await db.flush()
    request.delivery_status = await _send_pairing_telegram(request, approve_nonce)
    await write_audit(
        db,
        actor=Actor(kind="system", id="mcp-pairing"),
        source="mcp",
        action="mcp.pairing.start",
        outcome=request.delivery_status,
        metadata={"request_id": request.id, "agent_id": agent_id},
    )
    return PairingStart(request=request, pairing_secret=pairing_secret)


async def decide_pairing_by_nonce(
    db: AsyncSession,
    *,
    nonce: str,
    approve: bool,
    actor: Actor,
) -> McpPairingRequest:
    request = (
        await db.execute(
            select(McpPairingRequest).where(McpPairingRequest.approve_nonce_hash == _hash(nonce))
        )
    ).scalar_one_or_none()
    if request is None:
        raise McpClientError("unknown_pairing", "unknown MCP pairing request")
    if request.status != "pending":
        raise McpClientError("pairing_already_decided", f"MCP pairing already {request.status}")
    if _aware(request.expires_at) < utcnow():
        request.status = "expired"
        await write_audit(
            db,
            actor=actor,
            source="telegram",
            action="mcp.pairing.decide",
            outcome="expired",
            metadata={"request_id": request.id},
        )
        return request
    request.status = "approved" if approve else "denied"
    request.decided_by = actor.audit_id()
    if approve:
        request.approved_at = utcnow()
    else:
        request.denied_at = utcnow()
    await write_audit(
        db,
        actor=actor,
        source="telegram",
        action="mcp.pairing.decide",
        outcome=request.status,
        metadata={"request_id": request.id, "agent_id": request.agent_id},
    )
    return request


async def consume_pairing(
    db: AsyncSession,
    *,
    request_id: str,
    pairing_secret: str,
) -> PairingConsumeResult:
    request = await db.get(McpPairingRequest, request_id)
    if request is None:
        raise McpClientError("unknown_pairing", "unknown MCP pairing request")
    if request.status != "approved":
        if request.status == "pending" and _aware(request.expires_at) < utcnow():
            request.status = "expired"
        raise McpClientError("pairing_not_approved", f"MCP pairing is {request.status}")
    if request.consumed_at is not None:
        raise McpClientError("pairing_consumed", "MCP pairing was already consumed")
    if not secrets.compare_digest(request.pairing_secret_hash, _hash(pairing_secret)):
        raise McpClientError("invalid_pairing_secret", "invalid MCP pairing secret")

    token = f"hmc_{secrets.token_urlsafe(48)}"
    client = McpClient(
        agent_id=request.agent_id,
        client_label=request.client_label,
        host_fingerprint=request.host_fingerprint,
        token_hash=_hash(token),
        token_hint=_token_hint(token),
        approved_at=utcnow(),
        last_seen_at=utcnow(),
        created_by=request.decided_by,
    )
    db.add(client)
    request.consumed_at = utcnow()
    request.status = "consumed"
    await db.flush()
    await write_audit(
        db,
        actor=Actor(kind="system", id="mcp-pairing"),
        source="mcp",
        action="mcp.client.create",
        outcome="success",
        metadata={"client_id": client.id, "agent_id": client.agent_id},
    )
    return PairingConsumeResult(client=client, token=token)


async def create_client_token(
    db: AsyncSession,
    *,
    agent_id: str,
    client_label: str,
    host_fingerprint: str,
    actor: Actor,
    source: str = "telegram",
) -> PairingConsumeResult:
    agent_id = _clean_agent_id(agent_id)
    token = f"hmc_{secrets.token_urlsafe(48)}"
    client = McpClient(
        agent_id=agent_id,
        client_label=client_label[:128],
        host_fingerprint=host_fingerprint[:128],
        token_hash=_hash(token),
        token_hint=_token_hint(token),
        approved_at=utcnow(),
        last_seen_at=utcnow(),
        created_by=actor.audit_id(),
    )
    db.add(client)
    await db.flush()
    await write_audit(
        db,
        actor=actor,
        source=source,
        action="mcp.client.create",
        outcome="success",
        metadata={"client_id": client.id, "agent_id": client.agent_id},
    )
    return PairingConsumeResult(client=client, token=token)


async def validate_client_token(db: AsyncSession, *, token: str, agent_id: str) -> McpClient | None:
    agent_id = _clean_agent_id(agent_id)
    if not token:
        return None
    client = (
        await db.execute(select(McpClient).where(McpClient.token_hash == _hash(token)))
    ).scalar_one_or_none()
    if client is None or client.agent_id != agent_id or client.revoked_at is not None:
        return None
    client.last_seen_at = utcnow()
    await db.flush()
    return client


async def validate_client_token_any_agent(db: AsyncSession, *, token: str) -> McpClient | None:
    if not token:
        return None
    client = (
        await db.execute(select(McpClient).where(McpClient.token_hash == _hash(token)))
    ).scalar_one_or_none()
    if client is None or client.revoked_at is not None:
        return None
    client.last_seen_at = utcnow()
    await db.flush()
    return client


async def list_mcp_clients(db: AsyncSession) -> list[McpClient]:
    return list(
        (
            await db.execute(select(McpClient).order_by(McpClient.created_at.desc()))
        ).scalars()
    )


async def list_mcp_pairing_requests(db: AsyncSession, *, limit: int = 25) -> list[McpPairingRequest]:
    limit = max(1, min(limit, 100))
    return list(
        (
            await db.execute(
                select(McpPairingRequest).order_by(McpPairingRequest.created_at.desc()).limit(limit)
            )
        ).scalars()
    )


async def revoke_mcp_client(
    db: AsyncSession,
    *,
    client_id: str,
    reason: str,
    actor: Actor,
) -> McpClient:
    client = await db.get(McpClient, client_id)
    if client is None:
        raise McpClientError("unknown_client", "unknown MCP client")
    if client.revoked_at is None:
        client.revoked_at = utcnow()
        client.revoked_reason = reason[:256]
        await write_audit(
            db,
            actor=actor,
            source="rest",
            action="mcp.client.revoke",
            outcome="success",
            metadata={"client_id": client.id, "agent_id": client.agent_id},
        )
    return client


async def forget_mcp_client(
    db: AsyncSession,
    *,
    client_id: str,
    actor: Actor,
) -> None:
    client = await db.get(McpClient, client_id)
    if client is None:
        raise McpClientError("unknown_client", "unknown MCP client")
    if client.revoked_at is None:
        raise McpClientError("client_not_revoked", "revoke the MCP client before forgetting it")

    metadata = {"client_id": client.id, "agent_id": client.agent_id}
    await db.delete(client)
    await write_audit(
        db,
        actor=actor,
        source="rest",
        action="mcp.client.forget",
        outcome="success",
        metadata=metadata,
    )


async def rotate_mcp_client_token(
    db: AsyncSession,
    *,
    client_id: str,
    actor: Actor,
) -> PairingConsumeResult:
    client = await db.get(McpClient, client_id)
    if client is None:
        raise McpClientError("unknown_client", "unknown MCP client")
    if client.revoked_at is not None:
        raise McpClientError("client_revoked", "cannot rotate a revoked MCP client")
    token = f"hmc_{secrets.token_urlsafe(48)}"
    client.token_hash = _hash(token)
    client.token_hint = _token_hint(token)
    client.approved_at = utcnow()
    client.last_seen_at = None
    await db.flush()
    await write_audit(
        db,
        actor=actor,
        source="rest",
        action="mcp.client.rotate",
        outcome="success",
        metadata={"client_id": client.id, "agent_id": client.agent_id},
    )
    return PairingConsumeResult(client=client, token=token)


async def _send_pairing_telegram(request: McpPairingRequest, approve_nonce: str) -> str:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_allowed_chat_id:
        return "failed"
    text = (
        "MCP client registration request\n"
        f"Agent: {request.agent_id}\n"
        f"Client: {request.client_label or 'unknown'}\n"
        f"Host: {request.host_fingerprint or 'unknown'}\n"
        f"Request: {request.id[:8]}\n\n"
        f"Code: {approve_nonce}\n\n"
        f"Reply with /mcpapprove {approve_nonce} or /mcpdeny {approve_nonce} "
        "if Telegram buttons do not reach the webhook.\n\n"
        "Approve only if you started this MCP client."
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "Approve MCP", "callback_data": f"mcp_pairing:approve:{approve_nonce}"},
                {"text": "Deny MCP", "callback_data": f"mcp_pairing:deny:{approve_nonce}"},
            ]
        ]
    }
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": settings.telegram_allowed_chat_id,
                    "text": text,
                    "reply_markup": keyboard,
                },
            )
            body = response.json()
            if response.status_code == 200 and body.get("ok"):
                request.telegram_message_id = str(body.get("result", {}).get("message_id", ""))
                return "sent"
            return "failed"
    except httpx.HTTPError:
        return "failed"
