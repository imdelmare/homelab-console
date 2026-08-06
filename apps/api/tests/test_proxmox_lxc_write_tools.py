"""Focused tests for the governed Proxmox LXC write capabilities."""

import json
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from app.db.models import Approval, utcnow
from app.db.session import get_session_factory
from app.domain.actors import Actor
from app.providers.errors import ProviderError
from app.providers.proxmox import tools as proxmox_tools
from app.services.approvals_service import _approval_warning, input_digest
from app.tools.execution import execute_tool
from app.tools.governance import APPROVED_WRITE_TOOLS
from app.tools.registry import ProxmoxLxcPowerInput, get_tool

OPERATOR = Actor(kind="user", id="operator", label="operator")


def _configure(monkeypatch, *, critical_vmids=None):
    secrets = {
        "base_url": "https://pve.test:8006",
        "api_token_id": "console-reader@pve!test",
        "api_token_secret": "reader-secret",
        "power_api_token_id": "console-power@pve!test",
        "power_api_token_secret": "power-secret",
        "verify_tls": True,
    }
    config = {"critical_lxc_vmids": critical_vmids or []}
    monkeypatch.setattr(
        "app.providers.proxmox.client.get_provider_secrets",
        lambda _provider_id: secrets,
    )
    monkeypatch.setattr(
        "app.providers.proxmox.client.provider_config",
        lambda _provider_id: config,
    )
    monkeypatch.setattr(proxmox_tools, "provider_config", lambda _provider_id: config)


def _mock_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("verify", None)
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.httpclient.httpx.AsyncClient", client_factory)


def _resource(*, guest_type="lxc", status="running"):
    return {
        "type": guest_type,
        "vmid": 201,
        "name": "test-ct",
        "status": status,
        "node": "pve1",
    }


def test_lxc_tools_are_active_under_adr_0006():
    assert APPROVED_WRITE_TOOLS["proxmox.lxc.start"].endswith(
        "0006-activate-proxmox-lxc-drill.md"
    )
    assert APPROVED_WRITE_TOOLS["proxmox.lxc.shutdown"].endswith(
        "0006-activate-proxmox-lxc-drill.md"
    )
    start = get_tool("proxmox.lxc.start")
    shutdown = get_tool("proxmox.lxc.shutdown")
    assert start is not None and start.enabled is True
    assert shutdown is not None and shutdown.enabled is True


def test_lxc_tools_are_forced_disabled_without_governance_entries(monkeypatch):
    monkeypatch.delitem(APPROVED_WRITE_TOOLS, "proxmox.lxc.start")
    monkeypatch.delitem(APPROVED_WRITE_TOOLS, "proxmox.lxc.shutdown")
    start = get_tool("proxmox.lxc.start")
    shutdown = get_tool("proxmox.lxc.shutdown")
    assert start is not None and start.enabled is False
    assert shutdown is not None and shutdown.enabled is False


def test_lxc_input_is_strict_and_only_accepts_positive_vmid():
    assert ProxmoxLxcPowerInput.model_validate({"vmid": 201}).vmid == 201
    with pytest.raises(ValidationError):
        ProxmoxLxcPowerInput.model_validate({"vmid": 201, "node": "pve1"})
    with pytest.raises(ValidationError):
        ProxmoxLxcPowerInput.model_validate({"vmid": 0})


async def test_start_is_idempotent_and_only_reads_state(monkeypatch):
    _configure(monkeypatch)
    methods: list[str] = []

    def handler(request):
        methods.append(request.method)
        if request.url.path.endswith("/cluster/resources"):
            return httpx.Response(200, json={"data": [_resource(status="running")]})
        if request.url.path.endswith("/lxc/201/status/current"):
            return httpx.Response(
                200,
                json={"data": {"vmid": 201, "name": "test-ct", "status": "running"}},
            )
        return httpx.Response(404)

    _mock_http(monkeypatch, handler)
    result = await proxmox_tools.lxc_start(201)

    assert result["changed"] is False
    assert result["verified"] is True
    assert methods == ["GET", "GET"]


async def test_shutdown_uses_exact_graceful_endpoint_and_reads_back(monkeypatch):
    _configure(monkeypatch, critical_vmids=[201])
    captured: dict = {"reader_auth": [], "power_auth": []}

    def handler(request):
        path = request.url.path
        if path.endswith("/cluster/resources"):
            captured["reader_auth"].append(request.headers["authorization"])
            return httpx.Response(200, json={"data": [_resource(status="running")]})
        if path.endswith("/lxc/201/status/shutdown"):
            captured["method"] = request.method
            captured["body"] = json.loads(request.content.decode())
            captured["power_auth"].append(request.headers["authorization"])
            return httpx.Response(200, json={"data": "UPID:pve1:0001:shutdown:201:root:"})
        if "/tasks/" in path and path.endswith("/status"):
            captured["power_auth"].append(request.headers["authorization"])
            return httpx.Response(
                200, json={"data": {"status": "stopped", "exitstatus": "OK"}}
            )
        if path.endswith("/lxc/201/status/current"):
            captured["reader_auth"].append(request.headers["authorization"])
            return httpx.Response(
                200,
                json={"data": {"vmid": 201, "name": "test-ct", "status": "stopped"}},
            )
        return httpx.Response(404)

    _mock_http(monkeypatch, handler)
    result = await proxmox_tools.lxc_shutdown(201)

    assert captured["method"] == "POST"
    assert captured["body"] == {}
    assert all("console-reader@pve!test=reader-secret" in value for value in captured["reader_auth"])
    assert all("console-power@pve!test=power-secret" in value for value in captured["power_auth"])
    assert result["action"] == "shutdown"
    assert result["changed"] is True
    assert result["critical_target"] is True
    assert result["post_state"]["status"] == "stopped"
    assert result["verified"] is True


async def test_changed_action_requires_separate_power_credentials(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        "app.providers.proxmox.client.get_provider_secrets",
        lambda _provider_id: {
            "base_url": "https://pve.test:8006",
            "api_token_id": "console-reader@pve!test",
            "api_token_secret": "reader-secret",
            "verify_tls": True,
        },
    )
    def handler(request):
        if request.url.path.endswith("/cluster/resources"):
            return httpx.Response(200, json={"data": [_resource(status="stopped")]})
        return httpx.Response(500)

    _mock_http(monkeypatch, handler)
    with pytest.raises(ProviderError) as exc_info:
        await proxmox_tools.lxc_start(201)
    assert exc_info.value.code == "credentials_missing"
    assert "power API token" in exc_info.value.message


async def test_lxc_action_rejects_qemu_target(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        return httpx.Response(200, json={"data": [_resource(guest_type="qemu")]})

    _mock_http(monkeypatch, handler)
    with pytest.raises(ProviderError, match="not an LXC"):
        await proxmox_tools.lxc_shutdown(201)


async def test_lxc_action_rejects_missing_target(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        return httpx.Response(200, json={"data": []})

    _mock_http(monkeypatch, handler)
    with pytest.raises(ProviderError, match="not present"):
        await proxmox_tools.lxc_start(201)


async def test_failed_proxmox_task_is_normalized(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        path = request.url.path
        if path.endswith("/cluster/resources"):
            return httpx.Response(200, json={"data": [_resource(status="stopped")]})
        if path.endswith("/lxc/201/status/start"):
            return httpx.Response(200, json={"data": "UPID:pve1:0001:start:201:root:"})
        if "/tasks/" in path:
            return httpx.Response(
                200, json={"data": {"status": "stopped", "exitstatus": "ERROR"}}
            )
        return httpx.Response(404)

    _mock_http(monkeypatch, handler)
    with pytest.raises(ProviderError) as exc_info:
        await proxmox_tools.lxc_start(201)
    assert exc_info.value.code == "degraded"
    assert "UPID" not in exc_info.value.message


def test_shutdown_approval_highlights_configured_critical_lxc(monkeypatch):
    monkeypatch.setattr(
        "app.services.approvals_service.provider_config",
        lambda _provider_id: {"critical_lxc_vmids": [201]},
    )
    warning = _approval_warning("proxmox.lxc.shutdown", {"vmid": 201})
    assert "CRITICAL TARGET" in warning
    assert _approval_warning("proxmox.lxc.start", {"vmid": 201}) == ""
    assert _approval_warning("proxmox.lxc.shutdown", {"vmid": 202}) == ""


async def test_shutdown_approval_is_input_bound_and_single_use(monkeypatch):
    monkeypatch.setitem(
        APPROVED_WRITE_TOOLS,
        "proxmox.lxc.shutdown",
        "docs/decisions/0005-proxmox-lxc-write-capabilities.md",
    )

    async def fake_shutdown(vmid: int) -> dict:
        state = {"vmid": vmid, "name": "test-ct", "node": "pve1"}
        return {
            "action": "shutdown",
            "changed": True,
            "critical_target": False,
            "previous_state": {**state, "status": "running"},
            "post_state": {**state, "status": "stopped"},
            "verified": True,
        }

    monkeypatch.setattr(proxmox_tools, "lxc_shutdown", fake_shutdown)
    raw_input = {"vmid": 201}
    async with get_session_factory()() as db:
        approval = Approval(
            tool_id="proxmox.lxc.shutdown",
            status="approved",
            requested_by="operator",
            input_hash=input_digest(ProxmoxLxcPowerInput.model_validate(raw_input)),
            expires_at=utcnow() + timedelta(minutes=5),
        )
        db.add(approval)
        await db.commit()
        approval_id = approval.id

    wrong_target = await execute_tool(
        "proxmox.lxc.shutdown",
        {"vmid": 202},
        OPERATOR,
        approval_id=approval_id,
    )
    assert wrong_target.error is not None
    assert wrong_target.error.code == "approval_required"

    result = await execute_tool(
        "proxmox.lxc.shutdown",
        raw_input,
        OPERATOR,
        approval_id=approval_id,
    )
    assert result.ok is True
    assert result.result is not None
    assert result.result["post_state"]["status"] == "stopped"

    replay = await execute_tool(
        "proxmox.lxc.shutdown",
        raw_input,
        OPERATOR,
        approval_id=approval_id,
    )
    assert replay.error is not None
    assert replay.error.code == "approval_required"
