import pytest

from sqlalchemy import select

from app.db.models import AuditEvent, McpClient
from app.domain.actors import Actor
from app.services.mcp_clients import (
    McpClientError,
    consume_pairing,
    decide_pairing_by_nonce,
    forget_mcp_client,
    list_mcp_clients,
    mcp_client_public,
    revoke_mcp_client,
    rotate_mcp_client_token,
    start_pairing,
    validate_client_token,
)


OPERATOR = Actor(kind="telegram", id="111", label="telegram operator")


@pytest.fixture(autouse=True)
def _disable_telegram_delivery(monkeypatch):
    async def fake_send(_request, _nonce):
        return "sent"

    monkeypatch.setattr("app.services.mcp_clients._send_pairing_telegram", fake_send)


async def test_mcp_pairing_approves_consumes_and_validates_token(db_session):
    pairing = await start_pairing(
        db_session,
        agent_id="codex",
        client_label="Codex local",
        host_fingerprint="host-a",
    )
    await db_session.commit()

    try:
        await consume_pairing(
            db_session,
            request_id=pairing.request.id,
            pairing_secret=pairing.pairing_secret,
        )
    except McpClientError as exc:
        assert exc.code == "pairing_not_approved"
    else:
        raise AssertionError("unapproved pairing should not be consumable")

    request = await db_session.get(type(pairing.request), pairing.request.id)
    request.status = "approved"
    request.decided_by = OPERATOR.audit_id()
    await db_session.commit()

    consumed = await consume_pairing(
        db_session,
        request_id=pairing.request.id,
        pairing_secret=pairing.pairing_secret,
    )
    await db_session.commit()

    assert consumed.token.startswith("hmc_")
    assert consumed.client.token_hash != consumed.token
    assert consumed.client.token_hint == consumed.token[-8:]

    valid = await validate_client_token(db_session, token=consumed.token, agent_id="codex")
    await db_session.commit()
    assert valid is not None
    assert valid.id == consumed.client.id
    assert valid.last_seen_at is not None

    wrong_agent = await validate_client_token(db_session, token=consumed.token, agent_id="claude")
    assert wrong_agent is None


async def test_mcp_pairing_accepts_dedicated_fixer_identity(db_session):
    pairing = await start_pairing(
        db_session,
        agent_id="fixer",
        client_label="Fixer",
        host_fingerprint="claude",
    )
    pairing.request.status = "approved"
    pairing.request.decided_by = OPERATOR.audit_id()

    consumed = await consume_pairing(
        db_session,
        request_id=pairing.request.id,
        pairing_secret=pairing.pairing_secret,
    )
    await db_session.commit()

    assert consumed.client.agent_id == "fixer"
    assert await validate_client_token(
        db_session, token=consumed.token, agent_id="fixer"
    ) is not None
    assert await validate_client_token(db_session, token=consumed.token, agent_id="claude") is None


async def test_mcp_pairing_accepts_opencode_identity(db_session):
    pairing = await start_pairing(
        db_session,
        agent_id="opencode",
        client_label="OpenCode workstation",
        host_fingerprint="opencode-host",
    )
    pairing.request.status = "approved"
    pairing.request.decided_by = OPERATOR.audit_id()

    consumed = await consume_pairing(
        db_session,
        request_id=pairing.request.id,
        pairing_secret=pairing.pairing_secret,
    )
    await db_session.commit()

    assert consumed.client.agent_id == "opencode"
    assert await validate_client_token(
        db_session, token=consumed.token, agent_id="opencode"
    ) is not None
    assert await validate_client_token(db_session, token=consumed.token, agent_id="codex") is None


async def test_mcp_pairing_code_is_numeric_and_approves(db_session, monkeypatch):
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

    assert captured["code"].isdigit()
    assert len(captured["code"]) >= 6

    request = await decide_pairing_by_nonce(
        db_session,
        nonce=captured["code"],
        approve=True,
        actor=OPERATOR,
    )
    await db_session.commit()

    assert request.id == pairing.request.id
    assert request.status == "approved"


async def test_mcp_client_revocation_blocks_token(db_session):
    client = McpClient(
        agent_id="codex",
        client_label="Codex local",
        host_fingerprint="host-a",
        token_hash="hash",
        token_hint="hint",
    )
    db_session.add(client)
    await db_session.commit()

    revoked = await revoke_mcp_client(
        db_session,
        client_id=client.id,
        reason="lost laptop",
        actor=Actor(kind="user", id="operator", label="operator"),
    )
    await db_session.commit()

    assert revoked.revoked_at is not None
    assert revoked.revoked_reason == "lost laptop"
    assert "token_hash" not in mcp_client_public(revoked)


async def test_forget_mcp_client_requires_revocation_and_preserves_audit(db_session):
    client = McpClient(
        agent_id="codex",
        client_label="Old Codex",
        host_fingerprint="old-host",
        token_hash="forgotten-hash",
        token_hint="old-hint",
    )
    db_session.add(client)
    await db_session.commit()
    client_id = client.id

    with pytest.raises(McpClientError, match="revoke") as exc_info:
        await forget_mcp_client(db_session, client_id=client_id, actor=OPERATOR)
    assert exc_info.value.code == "client_not_revoked"
    assert await db_session.get(McpClient, client_id) is not None

    await revoke_mcp_client(
        db_session,
        client_id=client_id,
        reason="retired",
        actor=OPERATOR,
    )
    await forget_mcp_client(db_session, client_id=client_id, actor=OPERATOR)
    await db_session.commit()

    assert await db_session.get(McpClient, client_id) is None
    event = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.action == "mcp.client.forget")
        )
    ).scalar_one()
    assert event.outcome == "success"
    assert event.meta == {"client_id": client_id, "agent_id": "codex"}


async def test_mcp_client_rotation_invalidates_previous_token(db_session):
    created = await rotate_seed_client(db_session)

    rotated = await rotate_mcp_client_token(
        db_session,
        client_id=created.id,
        actor=Actor(kind="user", id="operator", label="operator"),
    )
    await db_session.commit()

    assert rotated.token.startswith("hmc_")
    assert rotated.client.token_hint == rotated.token[-8:]
    assert await validate_client_token(db_session, token="old-token", agent_id="codex") is None
    validated = await validate_client_token(db_session, token=rotated.token, agent_id="codex")
    assert validated is not None
    assert validated.id == created.id


async def rotate_seed_client(db_session):
    from app.services.mcp_clients import _hash

    client = McpClient(
        agent_id="codex",
        client_label="Codex local",
        host_fingerprint="host-a",
        token_hash=_hash("old-token"),
        token_hint="d-token",
    )
    db_session.add(client)
    await db_session.commit()
    return client


async def test_list_mcp_clients(db_session):
    db_session.add(
        McpClient(
            agent_id="claude",
            client_label="Claude VPS",
            host_fingerprint="host-b",
            token_hash="hash-2",
            token_hint="hint-2",
        )
    )
    await db_session.commit()

    clients = await list_mcp_clients(db_session)
    assert [client.agent_id for client in clients] == ["claude"]
