"""Async EMQX REST API v5 client.

Prefers dashboard API key/secret basic auth. Some EMQX 5.8 installs fail
API-key creation via the management API; for those, a dashboard username and
password can be used to obtain a short-lived bearer token for read-only calls.
"""

from typing import Any, Literal

import httpx

from app.providers.errors import ProviderError
from app.providers.httpclient import BaseJsonClient
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env


class EmqxClient(BaseJsonClient):
    provider_id = "emqx"

    def __init__(self) -> None:
        super().__init__()
        secrets = get_provider_secrets("emqx")
        config = provider_config("emqx")
        env = load_credentials_env()

        self.base_url = str(config.get("base_url") or secrets.get("base_url") or env.get("EMQX_URL") or "").rstrip("/")
        api_key = secrets.get("api_key") or env.get("EMQX_API_KEY") or ""
        api_secret = secrets.get("api_secret") or env.get("EMQX_API_SECRET") or ""
        self.auth = (api_key, api_secret) if api_key and api_secret else None
        self.username = secrets.get("username") or env.get("EMQX_USER") or ""
        self.password = secrets.get("password") or env.get("EMQX_PASSWORD") or ""
        self.verify_tls = bool(config.get("verify_tls", secrets.get("verify_tls", True)))
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 6.0)))

    def has_credentials(self) -> bool:
        return self.auth is not None or bool(self.username and self.password)

    def credentials_error(self) -> str:
        return "emqx api_key/api_secret or dashboard username/password is not configured"

    async def get(
        self,
        path: str,
        timeout: float | None = None,
        *,
        response_mode: Literal["json", "text", "auto"] = "json",
    ) -> Any:
        if self.auth is not None:
            return await super().get(path, timeout=timeout, response_mode=response_mode)
        if not self.is_configured():
            raise ProviderError("configuration_missing", "emqx base_url is not configured")
        if not self.has_credentials():
            raise ProviderError("credentials_missing", self.credentials_error())
        self._check_tls_policy()

        try:
            async with httpx.AsyncClient(
                verify=self.verify_tls,
                timeout=timeout or self.timeout_seconds,
            ) as client:
                login = await client.post(
                    f"{self.base_url}/api/v5/login",
                    json={"username": self.username, "password": self.password},
                )
                if login.status_code == 401:
                    raise ProviderError("auth_failed", "emqx rejected the dashboard credentials")
                if login.status_code >= 400:
                    raise ProviderError("degraded", f"emqx login returned HTTP {login.status_code}")
                token = (login.json() if login.headers.get("content-type", "").startswith("application/json") else {}).get("token")
                if not token:
                    raise ProviderError("invalid_response", "emqx login did not return a token")
                response = await client.get(
                    f"{self.base_url}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError("timeout", f"emqx did not respond within the timeout ({path})")
        except httpx.HTTPError as exc:
            raise ProviderError("unreachable", f"emqx request failed: {exc.__class__.__name__}")

        if response.status_code == 401:
            raise ProviderError("auth_failed", "emqx rejected the credentials")
        if response.status_code == 403:
            raise ProviderError("permission_denied", "emqx credentials lack permission")
        if response.status_code >= 500:
            raise ProviderError("degraded", f"emqx returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ProviderError("invalid_response", f"emqx returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ProviderError("invalid_response", "emqx returned a non-JSON response")
