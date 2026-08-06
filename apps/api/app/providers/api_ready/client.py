from app.providers.cloudflare_api import CloudflareApiClient
from app.providers.httpclient import BaseJsonClient
from app.services.inventory import ApiProviderInstanceEntry
from app.services.secrets import get_provider_secrets


class JsonHealthV1Client(BaseJsonClient):
    """Client for the fixed ``GET /health`` JSON health contract."""

    def __init__(self, instance: ApiProviderInstanceEntry) -> None:
        super().__init__()
        self.provider_id = instance.id
        self.base_url = instance.base_url.rstrip("/")
        self.verify_tls = instance.verify_tls
        self.timeout_seconds = instance.timeout_seconds
        token = str(get_provider_secrets(instance.id).get("bearer_token") or "")
        if token:
            self.headers = {"Authorization": f"Bearer {token}"}


class SpeedtestProbeV1Client(BaseJsonClient):
    """Client for the fixed home speedtest probe contract."""

    def __init__(self, instance: ApiProviderInstanceEntry) -> None:
        super().__init__()
        self.provider_id = instance.id
        self.base_url = instance.base_url.rstrip("/")
        self.verify_tls = instance.verify_tls
        self.timeout_seconds = instance.timeout_seconds
        token = str(get_provider_secrets(instance.id).get("bearer_token") or "")
        if token:
            self.headers = {"Authorization": f"Bearer {token}"}

    def has_credentials(self) -> bool:
        return "Authorization" in self.headers


class CloudflareTunnelV1Client(CloudflareApiClient):
    """Client for one declared tunnel through Cloudflare's official API."""

    def __init__(self, instance: ApiProviderInstanceEntry) -> None:
        super().__init__(
            provider_id=instance.id,
            account_id=instance.account_id,
            tunnel_ids=(instance.tunnel_id,),
            bearer_token=str(
                get_provider_secrets(instance.id).get("bearer_token") or ""
            ),
            timeout_seconds=instance.timeout_seconds,
        )
        self.tunnel_id = instance.tunnel_id

    async def tunnel_status(self, tunnel_id: str | None = None) -> object:
        return await super().tunnel_status(tunnel_id or self.tunnel_id)
