"""Asterisk provider and read-only tool implementations (AMI)."""

from datetime import UTC, datetime

from app.providers.asterisk import normalizers
from app.providers.asterisk.client import AsteriskClient
from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP


class AsteriskProvider(Provider):
    id = "asterisk"
    display_name = "Asterisk"
    credential_requirements = ("asterisk.host", "asterisk.username", "asterisk.secret")

    def client(self) -> AsteriskClient:
        return AsteriskClient()

    def ready(self) -> bool:
        client = self.client()
        return client.is_configured() and client.has_credentials()

    async def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        client = self.client()
        if not client.is_configured():
            return ProviderHealth(
                provider_id=self.id, status="unavailable",
                detail="not configured", checked_at=now,
            )
        if not client.has_credentials():
            return ProviderHealth(
                provider_id=self.id, status="misconfigured",
                detail="AMI username/secret not configured", checked_at=now,
            )
        try:
            await client.run_action("CoreStatus")
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def core_status() -> dict:
    status = (await AsteriskClient().run_action("CoreStatus"))["response"]
    settings = (await AsteriskClient().run_action("CoreSettings"))["response"]
    return {"core": normalizers.normalize_core(status, settings).model_dump()}


async def channels_list() -> dict:
    result = await AsteriskClient().run_action("CoreShowChannels", collect_events=True)
    channels = normalizers.normalize_channels(result["events"])
    return {"channels": [item.model_dump() for item in channels], "total": len(channels)}


async def peers_list() -> dict:
    """List SIP endpoints: PJSIP first, chan_sip as fallback."""
    client = AsteriskClient()
    try:
        result = await client.run_action("PJSIPShowEndpoints", collect_events=True)
        peers = normalizers.normalize_pjsip_endpoints(result["events"])
        return {"peers": [item.model_dump() for item in peers], "total": len(peers), "stack": "pjsip"}
    except ProviderError as exc:
        if exc.code != "invalid_response":
            raise

    result = await AsteriskClient().run_action("SIPpeers", collect_events=True)
    peers = normalizers.normalize_sip_peers(result["events"])
    return {"peers": [item.model_dump() for item in peers], "total": len(peers), "stack": "chan_sip"}
