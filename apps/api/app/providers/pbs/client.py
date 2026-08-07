"""Async Proxmox Backup Server API client.

Authentication is API-token only. Username/password login is intentionally
not supported.
"""

from typing import Any, Literal

from app.providers.errors import ProviderError
from app.providers.httpclient import BaseJsonClient
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env


class PbsClient(BaseJsonClient):
    provider_id = "pbs"

    def __init__(self) -> None:
        super().__init__()
        secrets = get_provider_secrets("pbs")
        config = provider_config("pbs")
        env = load_credentials_env()

        self.base_url = str(
            config.get("base_url")
            or secrets.get("base_url")
            or env.get("PBS_URL")
            or ""
        ).rstrip("/")
        self.token_id = secrets.get("api_token_id") or env.get("PBS_API_TOKEN_ID") or ""
        self.token_secret = secrets.get("api_token_secret") or env.get("PBS_API_TOKEN_SECRET") or ""
        self.headers = self.auth_header() if self.has_credentials() else {}
        self.verify_tls = bool(config.get("verify_tls", secrets.get("verify_tls", True)))
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 8.0)))

    def has_credentials(self) -> bool:
        return bool(self.token_id and self.token_secret)

    def credentials_error(self) -> str:
        return "pbs API token is not configured (api_token_id/api_token_secret)"

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"PBSAPIToken={self.token_id}:{self.token_secret}"}

    async def get(
        self,
        path: str,
        timeout: float | None = None,
        *,
        response_mode: Literal["json", "text", "auto"] = "json",
    ) -> Any:
        body = await super().get(path, timeout=timeout, response_mode=response_mode)
        if not isinstance(body, dict) or "data" not in body:
            raise ProviderError("invalid_response", "unexpected pbs response shape")
        return body["data"]
