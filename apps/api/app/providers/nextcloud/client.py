"""Async Nextcloud client: public status.php plus OCS API with app password."""

from app.providers.httpclient import BaseJsonClient
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env


class NextcloudClient(BaseJsonClient):
    provider_id = "nextcloud"

    def __init__(self) -> None:
        super().__init__()
        secrets = get_provider_secrets("nextcloud")
        config = provider_config("nextcloud")
        env = load_credentials_env()

        self.base_url = str(config.get("base_url") or secrets.get("base_url") or env.get("NEXTCLOUD_URL") or "").rstrip("/")
        username = secrets.get("username") or env.get("NEXTCLOUD_USER") or ""
        app_password = (
            secrets.get("app_password") or env.get("NEXTCLOUD_APP_PASSWORD") or ""
        )
        self.auth = (username, app_password) if username and app_password else None
        self.headers = {"OCS-APIRequest": "true", "Accept": "application/json"}
        self.verify_tls = bool(config.get("verify_tls", secrets.get("verify_tls", True)))
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 8.0)))

    def has_credentials(self) -> bool:
        return self.auth is not None

    def credentials_error(self) -> str:
        return "nextcloud username/app_password is not configured"
