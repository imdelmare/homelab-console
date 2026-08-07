from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.providers.errors import ProviderError
from app.providers.zerotier.client import (
    ZEROTIER_LEGACY_BASE_URL,
    ZeroTierClient,
)
from app.providers.zerotier.normalizers import normalize_members
from app.providers.zerotier.tools import (
    ZeroTierProvider,
    members_list,
    networks_list,
    status,
    summary,
)
from app.tools.registry import get_tool

NETWORK_ID = "0123456789abcdef"


def _configure(
    monkeypatch, *, network_ids=None, token="test-token", required_member_ids=None
):
    monkeypatch.setattr(
        "app.providers.zerotier.client.provider_config",
        lambda _provider_id: {
            "network_ids": [NETWORK_ID] if network_ids is None else network_ids,
            "timeout_seconds": 8,
            "offline_after_seconds": 600,
            "required_online_member_ids": required_member_ids or [],
            "verify_tls": False,
            "base_url": "https://attacker.invalid",
        },
    )
    monkeypatch.setattr(
        "app.providers.zerotier.client.get_provider_secrets",
        lambda _provider_id: {"api_token": token},
    )


def _mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        assert kwargs.pop("verify") is True
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.httpclient.httpx.AsyncClient", client_factory)


async def test_legacy_client_uses_fixed_origin_auth_and_declared_paths(monkeypatch):
    _configure(monkeypatch)
    paths: list[str] = []

    def handler(request: httpx.Request):
        assert str(request.url).startswith(ZEROTIER_LEGACY_BASE_URL)
        assert request.headers["authorization"] == "token test-token"
        paths.append(request.url.path)
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"status": "OK", "user": {"private": True}})
        if request.url.path.endswith("/member"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"id": NETWORK_ID, "config": {"name": "Lab"}})

    _mock_transport(monkeypatch, handler)

    assert (await status())["driver_id"] == "zerotier_central_legacy_v1"
    assert (await networks_list())["networks"][0]["name"] == "Lab"
    assert (await members_list())["total"] == 0
    assert paths == [
        "/api/v1/status",
        f"/api/v1/network/{NETWORK_ID}",
        f"/api/v1/network/{NETWORK_ID}/member",
    ]


async def test_client_rejects_undeclared_network_before_http(monkeypatch):
    _configure(monkeypatch)
    client = ZeroTierClient()

    with pytest.raises(ProviderError) as exc_info:
        await client.members("fedcba9876543210")

    assert exc_info.value.code == "configuration_missing"
    assert "fedcba" not in exc_info.value.message


@pytest.mark.parametrize(
    "network_ids",
    [["../../status"], ["not-hex-not-hex!"], "0123456789abcdef"],
)
def test_client_rejects_invalid_network_configuration(monkeypatch, network_ids):
    _configure(monkeypatch, network_ids=network_ids)

    with pytest.raises(ProviderError) as exc_info:
        ZeroTierClient()

    assert exc_info.value.code == "configuration_missing"


def test_client_rejects_invalid_required_member_configuration(monkeypatch):
    _configure(monkeypatch, required_member_ids=["not-a-member"])

    with pytest.raises(ProviderError) as exc_info:
        ZeroTierClient()

    assert exc_info.value.code == "configuration_missing"


async def test_members_list_counts_only_declared_always_on_members_as_required(
    monkeypatch,
):
    required_online = f"{NETWORK_ID}-0123456789"
    required_missing = f"{NETWORK_ID}-abcdef0123"
    _configure(
        monkeypatch,
        required_member_ids=[required_online, required_missing],
    )
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def handler(request: httpx.Request):
        assert request.url.path == f"/api/v1/network/{NETWORK_ID}/member"
        return httpx.Response(
            200,
            json=[
                {
                    "id": required_online,
                    "name": "Gateway",
                    "lastSeen": now_ms,
                    "config": {"authorized": True},
                },
                {
                    "id": f"{NETWORK_ID}-1111111111",
                    "name": "Phone",
                    "lastSeen": 1,
                    "config": {"authorized": True},
                },
            ],
        )

    _mock_transport(monkeypatch, handler)

    result = await members_list()

    assert result["stale"] == 1
    assert result["required_total"] == 2
    assert result["required_online"] == 1
    assert result["required_unavailable"] == 1
    assert result["required_missing"] == 1


async def test_summary_treats_intermittent_stale_members_as_informational(monkeypatch):
    async def healthy(self):
        return type("Health", (), {"status": "healthy", "detail": ""})()

    async def networks():
        return {"total": 1}

    async def members():
        return {
            "total": 5,
            "authorized": 5,
            "online": 2,
            "stale": 3,
            "unauthorized": 0,
            "required_total": 1,
            "required_online": 1,
            "required_unavailable": 0,
        }

    monkeypatch.setattr("app.providers.zerotier.tools.ZeroTierProvider.health", healthy)
    monkeypatch.setattr("app.providers.zerotier.tools.networks_list", networks)
    monkeypatch.setattr("app.providers.zerotier.tools.members_list", members)

    result = await summary()

    assert result["summary"]["status"] == "healthy"
    assert result["summary"]["findings"] == []
    assert result["summary"]["metrics"]["members_stale"] == 3


async def test_summary_allows_no_online_members_when_none_are_required(monkeypatch):
    async def healthy(self):
        return type("Health", (), {"status": "healthy", "detail": ""})()

    async def networks():
        return {"total": 1}

    async def members():
        return {
            "total": 5,
            "authorized": 5,
            "online": 0,
            "stale": 5,
            "unauthorized": 0,
            "required_total": 0,
            "required_online": 0,
            "required_unavailable": 0,
        }

    monkeypatch.setattr("app.providers.zerotier.tools.ZeroTierProvider.health", healthy)
    monkeypatch.setattr("app.providers.zerotier.tools.networks_list", networks)
    monkeypatch.setattr("app.providers.zerotier.tools.members_list", members)

    result = await summary()

    assert result["summary"]["status"] == "healthy"
    assert result["summary"]["findings"] == []


def test_member_normalizer_exposes_only_safe_fields_and_computes_staleness():
    now = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    fresh = int((now - timedelta(seconds=30)).timestamp() * 1000)
    stale = int((now - timedelta(minutes=30)).timestamp() * 1000)
    raw = [
        {
            "id": "member-one",
            "name": "Laptop",
            "lastSeen": fresh,
            "physicalAddress": "198.51.100.2",
            "config": {
                "authorized": True,
                "ipAssignments": ["10.147.17.2", "not-an-ip"],
                "identity": "secret-material",
            },
        },
        {
            "id": "member-two",
            "lastSeen": stale,
            "online": True,
            "config": {"authorized": True, "ipAssignments": ["fd00::2"]},
        },
    ]

    members = normalize_members(
        raw, NETWORK_ID, offline_after_seconds=600, now=now
    )
    dumped = [member.model_dump() for member in members]

    assert dumped[0]["online"] is True
    assert dumped[0]["assigned_ips"] == ["10.147.17.2"]
    assert dumped[1]["online"] is False
    assert dumped[1]["stale"] is True
    assert "physicalAddress" not in dumped[0]
    assert "identity" not in dumped[0]


async def test_health_distinguishes_missing_networks_and_token(monkeypatch):
    _configure(monkeypatch, network_ids=[])
    health = await ZeroTierProvider().health()
    assert health.status == "unavailable"

    _configure(monkeypatch, token="")
    health = await ZeroTierProvider().health()
    assert health.status == "misconfigured"


def test_registry_exposes_only_four_read_only_zerotier_tools():
    tool_ids = {
        "zerotier.status",
        "zerotier.networks.list",
        "zerotier.members.list",
        "zerotier.summary",
    }

    tools = [get_tool(tool_id) for tool_id in tool_ids]

    assert all(tool is not None for tool in tools)
    assert all(tool.provider_id == "zerotier" for tool in tools if tool)
    assert all(tool.mode == "read" and tool.risk == "low" for tool in tools if tool)
    assert all(tool.input_model.model_json_schema()["properties"] == {} for tool in tools if tool)
