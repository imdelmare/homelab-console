from app.services.provider_definitions import (
    SPECIAL_PROVIDER_IDS,
    STANDARD_HTTP_PROVIDER_IDS,
    STANDARD_PROVIDER_IDS,
    STANDARD_TCP_PROVIDER_IDS,
    list_provider_definitions,
)
from tests.conftest import do_login


def _cloudflare_instance():
    from app.services.inventory import ApiProviderInstanceEntry

    return ApiProviderInstanceEntry(
        id="cloudflare_home",
        name="Cloudflare Home Tunnel",
        driver="cloudflare_tunnel_v1",
        account_id="0123456789abcdef0123456789abcdef",
        tunnel_id="11111111-2222-4333-8444-555555555555",
    )


def test_definition_catalog_is_derived_for_explicit_standard_http_providers():
    definitions = list_provider_definitions()

    assert {item.id for item in definitions} == STANDARD_PROVIDER_IDS
    assert {item.id for item in definitions}.isdisjoint(SPECIAL_PROVIDER_IDS)
    proxmox = next(item for item in definitions if item.id == "proxmox")
    assert proxmox.transport == "http_json"
    assert "proxmox.cluster.status" in proxmox.capability_tool_ids
    assert "proxmox.cluster" in proxmox.observation_ids
    assert "proxmox.base_url" in proxmox.configuration_keys
    assert proxmox.supports_instances is False
    asterisk = next(item for item in definitions if item.id == "asterisk")
    nutups = next(item for item in definitions if item.id == "nutups")
    assert {asterisk.id, nutups.id} == STANDARD_TCP_PROVIDER_IDS
    assert asterisk.transport == "tcp_text"
    assert asterisk.driver_id == "asterisk_ami_v1"
    assert nutups.transport == "tcp_text"
    assert nutups.driver_id == "nut_upsd_v1"
    assert "asterisk.core.status" in asterisk.capability_tool_ids
    assert "nutups.status" in nutups.capability_tool_ids
    cloudflare = next(item for item in definitions if item.id == "cloudflaretunnel")
    assert cloudflare.transport == "http_json"
    assert cloudflare.driver_id == "cloudflare_tunnel_v1"
    assert cloudflare.capability_tool_ids == [
        "cloudflare.connectors.list",
        "cloudflare.summary",
        "cloudflare.tunnels.status",
    ]
    assert cloudflare.observation_ids == ["cloudflaretunnel.tunnel"]
    zerotier = next(item for item in definitions if item.id == "zerotier")
    assert zerotier.transport == "http_json"
    assert zerotier.driver_id == "zerotier_central_legacy_v1"
    assert zerotier.capability_tool_ids == [
        "zerotier.members.list",
        "zerotier.networks.list",
        "zerotier.status",
        "zerotier.summary",
    ]
    assert zerotier.observation_ids == ["zerotier.members"]


async def test_definition_catalog_endpoint_is_authenticated(
    client, user, capture_adapter
):
    unauthorized = await client.get("/api/provider-definitions")
    assert unauthorized.status_code == 401

    await do_login(client, capture_adapter)
    response = await client.get("/api/provider-definitions")

    assert response.status_code == 200
    assert {item["id"] for item in response.json()} == STANDARD_PROVIDER_IDS


def test_cloudflare_api_driver_is_a_standardized_configured_instance(monkeypatch):
    instance = _cloudflare_instance()
    monkeypatch.setattr(
        "app.services.provider_definitions.list_api_provider_instances",
        lambda: [instance],
    )
    monkeypatch.setattr(
        "app.providers.registry.list_api_provider_instances",
        lambda: [instance],
    )
    monkeypatch.setattr(
        "app.tools.registry.list_api_provider_instances",
        lambda: [instance],
    )

    definition = next(
        item for item in list_provider_definitions() if item.id == "cloudflare_home"
    )

    assert definition.transport == "http_json"
    assert definition.driver_id == "cloudflare_tunnel_v1"
    assert definition.supports_instances is True
    assert definition.configuration_keys == ["account_id", "tunnel_id", "bearer_token"]
    assert definition.capability_tool_ids == ["cloudflare_home.tunnel.status"]
