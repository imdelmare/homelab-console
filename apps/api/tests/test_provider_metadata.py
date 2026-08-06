from datetime import UTC, datetime
from typing import Literal

from app.db.models import ProviderConfiguration
from app.providers.base import ProviderHealth
from app.providers.registry import provider_health_snapshot
from app.services.provider_metadata import watcher_ids_for_provider
from tests.conftest import do_login


class StatefulProvider:
    id = "test-provider"
    display_name = "Test Provider"

    def __init__(self) -> None:
        self.status: Literal["healthy", "degraded"] = "degraded"
        self.detail = "normalized provider failure"

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.id,
            status=self.status,
            detail=self.detail,
            checked_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        )


async def test_provider_snapshot_preserves_last_normalized_error_after_recovery(db_session, monkeypatch):
    provider = StatefulProvider()
    monkeypatch.setattr("app.providers.registry.list_providers", lambda: [provider])

    await provider_health_snapshot(db_session)
    configuration = await db_session.get(ProviderConfiguration, provider.id)
    assert configuration is not None
    assert configuration.last_error_status == "degraded"
    assert configuration.last_error_detail == "normalized provider failure"
    assert configuration.last_error_at is not None

    provider.status = "healthy"
    provider.detail = ""
    await provider_health_snapshot(db_session)

    assert configuration.last_status == "healthy"
    assert configuration.last_ok_at is not None
    assert configuration.last_error_status == "degraded"
    assert configuration.last_error_detail == "normalized provider failure"


def test_provider_watcher_relationships_match_actual_inputs():
    assert watcher_ids_for_provider("opnsense") == [
        "lab.alerts",
        "network.gateway",
        "network.presence",
        "network.wireguard",
        "thermal.sensors",
    ]
    assert watcher_ids_for_provider("nutups") == ["power.ups", "thermal.sensors"]
    assert watcher_ids_for_provider("cloudflaretunnel") == ["cloudflare.tunnel"]
    assert watcher_ids_for_provider("zerotier") == ["network.zerotier"]
    assert watcher_ids_for_provider("proxmox") == ["lab.alerts", "thermal.sensors"]
    assert watcher_ids_for_provider("asterisk") == []


async def test_provider_endpoint_includes_tools_watchers_and_last_error(
    client,
    user,
    capture_adapter,
    db_session,
    monkeypatch,
):
    _, _csrf = await do_login(client, capture_adapter)
    observed_at = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    db_session.add(
        ProviderConfiguration(
            id="opnsense",
            display_name="OPNsense",
            last_status="healthy",
            last_error_status="unreachable",
            last_error_detail="normalized connection failure",
            last_error_at=observed_at,
        )
    )
    await db_session.commit()

    async def fake_snapshot(_db):
        return [
            ProviderHealth(
                provider_id="opnsense",
                status="healthy",
                checked_at=observed_at,
                last_ok_at=observed_at,
            )
        ]

    monkeypatch.setattr("app.api.routes_control.provider_health_snapshot", fake_snapshot)
    response = await client.get("/api/providers")

    assert response.status_code == 200, response.text
    provider = response.json()[0]
    assert provider["tool_count"] > 0
    assert provider["watchers"] == [
        "lab.alerts",
        "network.gateway",
        "network.presence",
        "network.wireguard",
        "thermal.sensors",
    ]
    assert provider["last_error"]["status"] == "unreachable"
    assert provider["last_error"]["message"] == "normalized connection failure"
    assert provider["last_error"]["at"].startswith("2026-07-16T12:00:00")
