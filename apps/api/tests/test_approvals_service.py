from datetime import timedelta

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.settings import get_settings
from app.db.models import AuditEvent, utcnow
from app.domain.actors import Actor
from app.services import approvals_service, telegram_service
from app.services.approvals_service import (
    ApprovalError,
    decide_approval,
    list_approvals,
    request_approval,
)
from app.tools import registry
from app.tools.execution import execute_tool
from app.tools.governance import APPROVED_WRITE_TOOLS
from app.tools.registry import EmptyInput, ToolDefinition

OPERATOR = Actor(kind="user", id="operator", label="operator")
AGENT = Actor(kind="agent", id="fixer", label="fixer")
TELEGRAM_OPERATOR = Actor(kind="telegram", id="42", label="telegram operator")


class PauseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_minutes: int = 5


def _fake_write_tool(monkeypatch, **overrides) -> ToolDefinition:
    async def runner(_payload):
        return {"answer": 42}

    defaults = dict(
        id="test.write",
        name="Test Write",
        description="test write tool",
        provider_id="test",
        category="test",
        mode="write",
        risk="low",
        enabled=True,
        timeout_seconds=5.0,
        input_model=PauseInput,
        runner=runner,
    )
    defaults.update(overrides)
    tool = ToolDefinition.model_validate(defaults)
    monkeypatch.setattr(registry, "_TOOLS", [*registry._TOOLS, tool])
    monkeypatch.setitem(APPROVED_WRITE_TOOLS, tool.id, "docs/decisions/0004.md")
    return tool


@pytest.fixture
def telegram_recorder(monkeypatch):
    sent: list[tuple[str, str, dict | None]] = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text, reply_markup))
        return True

    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_allowed_chat_id", "777")
    monkeypatch.setattr(telegram_service, "send_message", fake_send)
    return sent


async def test_request_creates_pending_and_notifies(db_session, monkeypatch, telegram_recorder):
    _fake_write_tool(monkeypatch)

    approval = await request_approval(
        db_session,
        tool_id="test.write",
        raw_input={"duration_minutes": 15},
        actor=AGENT,
        source="mcp",
    )
    await db_session.commit()

    assert approval.status == "pending"
    assert len(approval.input_hash) == 64
    assert "test.write" in approval.action
    assert approval.requested_by == AGENT.audit_id()

    assert len(telegram_recorder) == 1
    chat_id, text, keyboard = telegram_recorder[0]
    assert chat_id == "777"
    assert "Write approval request" in text
    assert "From: agent:fixer" in text
    assert "test.write" in text
    assert "duration_minutes=15" in text
    callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
    assert f"approval:approve:{approval.id}" in callbacks
    assert f"approval:deny:{approval.id}" in callbacks
    labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
    assert labels == ["✅ Approve", "⛔ Deny"]

    audit = await db_session.execute(
        select(AuditEvent.action, AuditEvent.outcome).where(
            AuditEvent.action.in_(("approval.request", "approval.notify"))
        )
    )
    outcomes = dict(audit.all())
    assert outcomes["approval.request"] == "pending"
    assert outcomes["approval.notify"] == "sent"


async def test_request_rejects_non_approvable_and_invalid_input(db_session, monkeypatch):
    _fake_write_tool(monkeypatch, id="test.read", mode="read", input_model=EmptyInput)

    with pytest.raises(ApprovalError) as excinfo:
        await request_approval(db_session, tool_id="test.read", raw_input={}, actor=OPERATOR)
    assert excinfo.value.code == "not_approvable"

    _fake_write_tool(monkeypatch)
    with pytest.raises(ApprovalError) as excinfo:
        await request_approval(
            db_session, tool_id="test.write", raw_input={"bogus": 1}, actor=OPERATOR
        )
    assert excinfo.value.code == "invalid_input"

    with pytest.raises(ApprovalError) as excinfo:
        await request_approval(db_session, tool_id="no.such", raw_input={}, actor=OPERATOR)
    assert excinfo.value.code == "unknown_tool"


async def test_full_lifecycle_request_approve_execute(db_session, monkeypatch, telegram_recorder):
    _fake_write_tool(monkeypatch)
    raw_input = {"duration_minutes": 30}

    approval = await request_approval(
        db_session, tool_id="test.write", raw_input=raw_input, actor=AGENT, source="mcp"
    )
    await db_session.commit()

    decided, outcome = await decide_approval(
        db_session, approval_id=approval.id, approve=True, actor=TELEGRAM_OPERATOR
    )
    await db_session.commit()
    assert outcome == "approved"
    assert decided.decided_by == TELEGRAM_OPERATOR.audit_id()

    result = await execute_tool("test.write", raw_input, AGENT, approval_id=approval.id)
    assert result.ok is True

    replay = await execute_tool("test.write", raw_input, AGENT, approval_id=approval.id)
    assert replay.ok is False
    assert replay.error is not None
    assert replay.error.code == "approval_required"


async def test_decide_deny_replay_and_expiry(db_session, monkeypatch, telegram_recorder):
    _fake_write_tool(monkeypatch)

    denied_approval = await request_approval(
        db_session, tool_id="test.write", raw_input={}, actor=OPERATOR
    )
    _, outcome = await decide_approval(
        db_session, approval_id=denied_approval.id, approve=False, actor=TELEGRAM_OPERATOR
    )
    assert outcome == "denied"
    _, outcome = await decide_approval(
        db_session, approval_id=denied_approval.id, approve=True, actor=TELEGRAM_OPERATOR
    )
    assert outcome == "replayed"

    expired_approval = await request_approval(
        db_session, tool_id="test.write", raw_input={}, actor=OPERATOR
    )
    expired_approval.expires_at = utcnow() - timedelta(minutes=1)
    await db_session.commit()
    decided, outcome = await decide_approval(
        db_session, approval_id=expired_approval.id, approve=True, actor=TELEGRAM_OPERATOR
    )
    assert outcome == "expired"
    assert decided.status == "expired"

    with pytest.raises(ApprovalError):
        await decide_approval(
            db_session, approval_id="missing", approve=True, actor=TELEGRAM_OPERATOR
        )


async def test_list_approvals_filters_by_status(db_session, monkeypatch, telegram_recorder):
    _fake_write_tool(monkeypatch)
    first = await request_approval(db_session, tool_id="test.write", raw_input={}, actor=OPERATOR)
    await request_approval(db_session, tool_id="test.write", raw_input={}, actor=OPERATOR)
    await decide_approval(db_session, approval_id=first.id, approve=False, actor=TELEGRAM_OPERATOR)
    await db_session.commit()

    pending = await list_approvals(db_session, status="pending")
    assert {approval.status for approval in pending} == {"pending"}
    everything = await list_approvals(db_session)
    assert len(everything) >= 2


async def test_notify_not_delivered_without_telegram_config(db_session, monkeypatch):
    _fake_write_tool(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "")

    approval = await request_approval(
        db_session, tool_id="test.write", raw_input={}, actor=OPERATOR
    )
    assert approval.status == "pending"
    audit = await db_session.execute(
        select(AuditEvent.outcome).where(AuditEvent.action == "approval.notify")
    )
    assert audit.scalar_one() == "not_delivered"
