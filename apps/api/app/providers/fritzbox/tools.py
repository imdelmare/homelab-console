"""FritzBox provider and read-only tool implementations."""

from datetime import UTC, datetime

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.fritzbox import normalizers
from app.providers.fritzbox.client import FritzBoxClient, FritzBoxWebSession, TARGETS
from app.providers.httpclient import HEALTH_STATUS_MAP


class FritzBoxProvider(Provider):
    def __init__(self, provider_id: str) -> None:
        self.id = provider_id
        self.display_name = TARGETS[provider_id].display_name
        self.credential_requirements = (
            f"{provider_id}.base_url",
            f"{provider_id}.username",
            f"{provider_id}.password",
        )

    def client(self) -> FritzBoxClient:
        return FritzBoxClient(self.id)

    def ready(self) -> bool:
        client = self.client()
        return client.is_configured()

    async def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        client = self.client()
        if not client.is_configured():
            return ProviderHealth(provider_id=self.id, status="unavailable", detail="not configured", checked_at=now)
        try:
            await client.get_description()
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def device_info(provider_id: str) -> dict:
    raw = await FritzBoxClient(provider_id).call("DeviceInfo", "GetInfo")
    return {"device": normalizers.normalize_device_info(raw).model_dump()}


async def wan_status(provider_id: str) -> dict:
    client = FritzBoxClient(provider_id)
    try:
        properties = await client.call("WANCommonInterfaceConfig", "GetCommonLinkProperties")
    except ProviderError as exc:
        if exc.code == "invalid_response" and "WANCommonInterfaceConfig" in exc.message:
            return {"wan": {}, "available": False, "note": "WANCommonInterfaceConfig is not exposed by this FritzBox"}
        raise
    sent = await client.call("WANCommonInterfaceConfig", "GetTotalBytesSent")
    received = await client.call("WANCommonInterfaceConfig", "GetTotalBytesReceived")
    return {"wan": normalizers.normalize_wan(properties, sent, received).model_dump()}


async def wifi_summary(provider_id: str) -> dict:
    client = FritzBoxClient(provider_id)
    radios = []
    for index in (1, 2, 3):
        try:
            raw = await client.call("WLANConfiguration", "GetInfo", index=index)
        except ProviderError as exc:
            if exc.code in {"invalid_response", "permission_denied"}:
                continue
            raise
        if not raw:
            continue
        radios.append(normalizers.normalize_wifi_radio(index, raw))
    return {"wifi": [item.model_dump() for item in radios], "total": len(radios)}


async def system_temperature(provider_id: str) -> dict:
    session = FritzBoxWebSession(FritzBoxClient(provider_id))
    raw = await session.get_ecostat()
    return {"temperature": normalizers.normalize_ecostat_temperature(raw).model_dump()}
