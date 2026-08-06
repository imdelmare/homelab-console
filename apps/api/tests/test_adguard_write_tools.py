"""Focused tests for the ADR 0004 AdGuard write pilot: provider behavior,
registry gating and the approval-bound execution path."""

import json
from datetime import timedelta

import httpx

from app.db.models import Approval, utcnow
from app.db.session import get_session_factory
from app.domain.actors import Actor
from app.providers.adguard import tools as adguard_tools
from app.services.approvals_service import input_digest
from app.tools import execution as execution_module
from app.tools.execution import execute_tool
from app.tools.governance import APPROVED_WRITE_TOOLS
from app.tools.registry import AdguardPauseInput, get_tool

OPERATOR = Actor(kind="user", id="operator", label="operator")


def _configure_adguard(monkeypatch):
    secrets = {"base_url": "http://adguard.test", "username": "u", "password": "p"}
    monkeypatch.setattr(
        "app.providers.adguard.client.get_provider_secrets", lambda _pid: secrets, raising=True
    )
    monkeypatch.setattr(
        "app.providers.adguard.client.provider_config", lambda _pid: {}, raising=True
    )
    monkeypatch.setattr(
        "app.providers.adguard.client.load_credentials_env", lambda: {}, raising=True
    )


def _mock_adguard_http(monkeypatch, *, protection_enabled_after: bool, duration_ms: int = 0):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control/protection":
            captured["method"] = request.method
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, text="OK")
        if request.url.path == "/control/status":
            return httpx.Response(
                200,
                json={
                    "protection_enabled": protection_enabled_after,
                    "protection_disabled_duration": duration_ms,
                    "version": "v0.107.78",
                    "running": True,
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("verify", None)
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.httpclient.httpx.AsyncClient", client_factory)
    return captured


async def test_protection_pause_posts_bounded_duration_and_reads_back(monkeypatch):
    _configure_adguard(monkeypatch)
    captured = _mock_adguard_http(
        monkeypatch, protection_enabled_after=False, duration_ms=900_000
    )

    result = await adguard_tools.protection_pause(15)

    assert captured["method"] == "POST"
    assert captured["body"] == {"enabled": False, "duration": 900_000}
    assert result["verified"] is True
    assert result["post_state"]["protection_enabled"] is False
    assert result["requested_duration_minutes"] == 15


async def test_protection_resume_reenables_and_reads_back(monkeypatch):
    _configure_adguard(monkeypatch)
    captured = _mock_adguard_http(monkeypatch, protection_enabled_after=True)

    result = await adguard_tools.protection_resume()

    assert captured["body"] == {"enabled": True}
    assert result["verified"] is True
    assert result["post_state"]["protection_enabled"] is True


async def test_pause_not_verified_when_provider_state_disagrees(monkeypatch):
    _configure_adguard(monkeypatch)
    _mock_adguard_http(monkeypatch, protection_enabled_after=True)

    result = await adguard_tools.protection_pause(5)
    assert result["verified"] is False


def test_adguard_writes_active_under_adr_0004():
    assert APPROVED_WRITE_TOOLS["adguard.protection.pause"].endswith(
        "0004-first-live-write-capabilities.md"
    )
    pause = get_tool("adguard.protection.pause")
    resume = get_tool("adguard.protection.resume")
    assert pause is not None and pause.enabled is True
    assert resume is not None and resume.enabled is True


def test_adguard_writes_forced_disabled_without_allowlist_entry(monkeypatch):
    monkeypatch.delitem(APPROVED_WRITE_TOOLS, "adguard.protection.pause")
    monkeypatch.delitem(APPROVED_WRITE_TOOLS, "adguard.protection.resume")
    pause = get_tool("adguard.protection.pause")
    resume = get_tool("adguard.protection.resume")
    assert pause is not None and pause.enabled is False
    assert resume is not None and resume.enabled is False


def _activate(monkeypatch):
    monkeypatch.setitem(APPROVED_WRITE_TOOLS, "adguard.protection.pause", "docs/decisions/0004.md")
    monkeypatch.setitem(APPROVED_WRITE_TOOLS, "adguard.protection.resume", "docs/decisions/0004.md")
    monkeypatch.setattr(
        execution_module,
        "medium_risk_allowlist",
        lambda: ["adguard.protection.pause"],
    )


async def test_pause_requires_medium_policy_and_approval(monkeypatch):
    monkeypatch.setitem(APPROVED_WRITE_TOOLS, "adguard.protection.pause", "docs/decisions/0004.md")
    monkeypatch.setattr(execution_module, "medium_risk_allowlist", lambda: [])
    denied = await execute_tool("adguard.protection.pause", {"duration_minutes": 5}, OPERATOR)
    assert denied.error is not None
    assert denied.error.code == "policy_denied"

    _activate(monkeypatch)
    unapproved = await execute_tool("adguard.protection.pause", {"duration_minutes": 5}, OPERATOR)
    assert unapproved.error is not None
    assert unapproved.error.code == "approval_required"


async def test_pause_rejects_unlisted_duration(monkeypatch):
    _activate(monkeypatch)
    result = await execute_tool("adguard.protection.pause", {"duration_minutes": 7}, OPERATOR)
    assert result.error is not None
    assert result.error.code == "invalid_input"


async def test_approved_pause_runs_and_consumes_input_bound_approval(monkeypatch):
    _activate(monkeypatch)

    async def fake_pause(duration_minutes: int) -> dict:
        return {
            "requested_duration_minutes": duration_minutes,
            "post_state": {"protection_enabled": False, "disabled_duration_ms": 300_000},
            "verified": True,
        }

    monkeypatch.setattr(adguard_tools, "protection_pause", fake_pause)

    raw_input = {"duration_minutes": 5}
    async with get_session_factory()() as db:
        approval = Approval(
            tool_id="adguard.protection.pause",
            status="approved",
            requested_by="operator",
            input_hash=input_digest(AdguardPauseInput.model_validate(raw_input)),
            expires_at=utcnow() + timedelta(minutes=5),
        )
        db.add(approval)
        await db.commit()
        approval_id = approval.id

    wrong_input = await execute_tool(
        "adguard.protection.pause", {"duration_minutes": 60}, OPERATOR, approval_id=approval_id
    )
    assert wrong_input.error is not None
    assert wrong_input.error.code == "approval_required"

    result = await execute_tool(
        "adguard.protection.pause", raw_input, OPERATOR, approval_id=approval_id
    )
    assert result.ok is True
    assert result.result is not None
    assert result.result["verified"] is True
    assert result.result["post_state"]["protection_enabled"] is False

    replay = await execute_tool(
        "adguard.protection.pause", raw_input, OPERATOR, approval_id=approval_id
    )
    assert replay.error is not None
    assert replay.error.code == "approval_required"
