"""Async Uptime Kuma client.

Uptime Kuma exposes no general REST API; monitor state is read from the
Prometheus /metrics endpoint (API key via basic auth) and from public
status-page JSON endpoints.
"""

from app.providers.httpclient import BaseJsonClient
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env


class UptimeKumaClient(BaseJsonClient):
    provider_id = "uptimekuma"

    def __init__(self) -> None:
        super().__init__()
        secrets = get_provider_secrets("uptimekuma")
        config = provider_config("uptimekuma")
        env = load_credentials_env()

        self.base_url = str(config.get("base_url") or secrets.get("base_url") or env.get("UPTIMEKUMA_URL") or "").rstrip("/")
        api_key = secrets.get("api_key") or env.get("UPTIMEKUMA_API_KEY") or ""
        # Uptime Kuma expects the API key as the basic-auth password.
        self.auth = ("", api_key) if api_key else None
        self.verify_tls = bool(config.get("verify_tls", secrets.get("verify_tls", True)))
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 6.0)))
    def has_credentials(self) -> bool:
        # Status-page endpoints are public; only /metrics needs the API key.
        return True

    def has_api_key(self) -> bool:
        return self.auth is not None
