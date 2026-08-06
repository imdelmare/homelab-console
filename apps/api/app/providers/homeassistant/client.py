"""Async Home Assistant REST API client."""

from app.providers.httpclient import BaseJsonClient
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env


class HomeAssistantClient(BaseJsonClient):
    provider_id = "homeassistant"

    def __init__(self) -> None:
        super().__init__()
        secrets = get_provider_secrets("homeassistant")
        config = provider_config("homeassistant")
        env = load_credentials_env()

        self.base_url = str(
            config.get("base_url")
            or secrets.get("base_url")
            or env.get("HOMEASSISTANT_URL")
            or env.get("HA_URL")
            or ""
        ).rstrip("/")
        self.token = secrets.get("token") or env.get("HOMEASSISTANT_TOKEN") or env.get("HA_TOKEN") or ""
        if self.token:
            self.headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        self.verify_tls = bool(config.get("verify_tls", secrets.get("verify_tls", True)))
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 8.0)))
    def has_credentials(self) -> bool:
        return bool(self.token)

    def credentials_error(self) -> str:
        return "homeassistant token is not configured"
