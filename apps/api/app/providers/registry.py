import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProviderConfiguration, utcnow
from app.providers.adguard.tools import AdguardProvider
from app.providers.asterisk.tools import AsteriskProvider
from app.providers.base import Provider, ProviderHealth
from app.providers.cloudflaretunnel.tools import CloudflareTunnelProvider
from app.providers.emqx.tools import EmqxProvider
from app.providers.frigate.tools import FrigateProvider
from app.providers.fritzbox.tools import FritzBoxProvider
from app.providers.homeassistant.tools import HomeAssistantProvider
from app.providers.glances.tools import GlancesProvider
from app.providers.mikrotik.tools import MikrotikProvider
from app.providers.nextcloud.tools import NextcloudProvider
from app.providers.nutups.tools import NutUpsProvider
from app.providers.opnsense.tools import OpnsenseProvider
from app.providers.pbs.tools import PbsProvider
from app.providers.proxmox.tools import ProxmoxProvider
from app.providers.uptimekuma.tools import UptimeKumaProvider
from app.providers.vps.tools import VpsProvider
from app.providers.zerotier.tools import ZeroTierProvider
from app.providers.api_ready.tools import ApiReadyProvider
from app.services.inventory import list_api_provider_instances
from app.services.redaction import redact

_PROVIDERS: dict[str, Provider] = {}
RESERVED_PROVIDER_IDS = frozenset({"console", "network"})


def register(provider: Provider) -> None:
    _PROVIDERS[provider.id] = provider


def get_provider(provider_id: str) -> Provider | None:
    provider = _PROVIDERS.get(provider_id)
    if provider is not None:
        return provider
    if provider_id in RESERVED_PROVIDER_IDS:
        return None
    instance = next(
        (item for item in list_api_provider_instances() if item.id == provider_id),
        None,
    )
    return ApiReadyProvider(instance) if instance is not None else None


def list_providers() -> list[Provider]:
    configured = [
        ApiReadyProvider(instance)
        for instance in list_api_provider_instances()
        if instance.id not in _PROVIDERS and instance.id not in RESERVED_PROVIDER_IDS
    ]
    return [*_PROVIDERS.values(), *configured]


register(ProxmoxProvider())
register(PbsProvider())
register(OpnsenseProvider())
register(MikrotikProvider())
register(HomeAssistantProvider())
register(FrigateProvider())
register(AdguardProvider())
register(NextcloudProvider())
register(NutUpsProvider())
register(AsteriskProvider())
register(UptimeKumaProvider())
register(EmqxProvider())
register(CloudflareTunnelProvider())
register(VpsProvider())
register(ZeroTierProvider())
register(FritzBoxProvider("fritzbox_primary"))
register(FritzBoxProvider("fritzbox_secondary"))
register(GlancesProvider())


async def provider_health_snapshot(db: AsyncSession) -> list[ProviderHealth]:
    """Probe every provider concurrently and persist last status / last
    successful observation timestamp and last normalized failure."""
    providers = list_providers()
    healths = await asyncio.gather(*(provider.health() for provider in providers))

    snapshots: list[ProviderHealth] = []
    for provider, health in zip(providers, healths):
        row = await db.get(ProviderConfiguration, provider.id)
        if row is None:
            row = ProviderConfiguration(id=provider.id, display_name=provider.display_name)
            db.add(row)
        observed_at = health.checked_at or utcnow()
        health.checked_at = observed_at
        row.last_status = health.status
        row.updated_at = observed_at
        if health.status == "healthy":
            row.last_ok_at = observed_at
        else:
            safe_detail = redact({"detail": health.detail}).get("detail", "")
            row.last_error_status = health.status
            row.last_error_detail = str(safe_detail or f"Provider reported {health.status}")[:2000]
            row.last_error_at = observed_at
        health.last_ok_at = row.last_ok_at
        snapshots.append(health)
    await db.flush()
    return snapshots
