"""Async Frigate API client."""

from app.providers.httpclient import BaseJsonClient
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env


class FrigateClient(BaseJsonClient):
    provider_id = "frigate"

    def __init__(self) -> None:
        super().__init__()
        secrets = get_provider_secrets("frigate")
        config = provider_config("frigate")
        env = load_credentials_env()

        host = env.get("FRIGATE_HOST") or ""
        self.base_url = str(
            config.get("base_url")
            or secrets.get("base_url")
            or env.get("FRIGATE_URL")
            or (f"http://{host}:5000" if host else "")
        ).rstrip("/")
        token = secrets.get("token") or env.get("FRIGATE_TOKEN") or ""
        api_key = secrets.get("api_key") or env.get("FRIGATE_API_KEY") or ""
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        if api_key:
            self.headers["X-API-Key"] = api_key
        self.verify_tls = bool(config.get("verify_tls", secrets.get("verify_tls", True)))
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 8.0)))
