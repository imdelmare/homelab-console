"""Async Proxmox VE API client.

Authentication is API-token only. Username/password login is intentionally
not supported. TLS verification defaults to on; disabling it is allowed only
outside live mode and logs a loud warning.
"""

from typing import Any, Literal

from app.providers.errors import ProviderError
from app.providers.httpclient import BaseJsonClient
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env


class ProxmoxClient(BaseJsonClient):
    provider_id = "proxmox"

    def __init__(self, credential_profile: Literal["reader", "power"] = "reader") -> None:
        super().__init__()
        secrets = get_provider_secrets("proxmox")
        config = provider_config("proxmox")
        env = load_credentials_env()
        self.credential_profile = credential_profile

        self.base_url = str(
            config.get("base_url")
            or secrets.get("base_url")
            or env.get("PROXMOX_URL")
            or (f"https://{env['PROXMOX_HOST']}:8006" if env.get("PROXMOX_HOST") else "")
        ).rstrip("/")
        if credential_profile == "power":
            self.token_id = (
                secrets.get("power_api_token_id")
                or env.get("PROXMOX_POWER_API_TOKEN_ID")
                or ""
            )
            self.token_secret = (
                secrets.get("power_api_token_secret")
                or env.get("PROXMOX_POWER_API_TOKEN_SECRET")
                or ""
            )
        else:
            self.token_id = secrets.get("api_token_id") or env.get("PROXMOX_API_TOKEN_ID") or ""
            self.token_secret = (
                secrets.get("api_token_secret") or env.get("PROXMOX_API_TOKEN_SECRET") or ""
            )
        self.headers = self.auth_header() if self.has_credentials() else {}
        self.verify_tls = bool(config.get("verify_tls", secrets.get("verify_tls", True)))
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 6.0)))

    def has_credentials(self) -> bool:
        return bool(self.token_id and self.token_secret)

    def credentials_error(self) -> str:
        if self.credential_profile == "power":
            return (
                "proxmox power API token is not configured "
                "(power_api_token_id/power_api_token_secret)"
            )
        return "proxmox API token is not configured (api_token_id/api_token_secret)"

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"PVEAPIToken={self.token_id}={self.token_secret}"}

    async def get(
        self,
        path: str,
        timeout: float | None = None,
        *,
        response_mode: Literal["json", "text", "auto"] = "json",
    ) -> Any:
        body = await super().get(path, timeout=timeout, response_mode=response_mode)
        return self._response_data(body)

    async def post(
        self,
        path: str,
        timeout: float | None = None,
        *,
        response_mode: Literal["json", "text", "auto"] = "json",
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        body = await super().post(
            path,
            timeout=timeout,
            response_mode=response_mode,
            json_body=json_body,
        )
        return self._response_data(body)

    @staticmethod
    def _response_data(body: Any) -> Any:
        if not isinstance(body, dict) or "data" not in body:
            raise ProviderError("invalid_response", "unexpected proxmox response shape")
        return body["data"]
