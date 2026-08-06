"""Async AdGuard Home API client (basic auth)."""

from app.providers.httpclient import BaseJsonClient
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env


class AdguardClient(BaseJsonClient):
    provider_id = "adguard"

    def __init__(self) -> None:
        super().__init__()
        secrets = get_provider_secrets("adguard")
        config = provider_config("adguard")
        env = load_credentials_env()

        self.base_url = str(config.get("base_url") or secrets.get("base_url") or env.get("ADGUARD_URL") or "").rstrip("/")
        username = secrets.get("username") or env.get("ADGUARD_USER") or ""
        password = secrets.get("password") or env.get("ADGUARD_PASSWORD") or ""
        self.auth = (username, password) if username and password else None
        self.verify_tls = bool(config.get("verify_tls", secrets.get("verify_tls", True)))
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 6.0)))

    def has_credentials(self) -> bool:
        return self.auth is not None

    def credentials_error(self) -> str:
        return "adguard username/password is not configured"
