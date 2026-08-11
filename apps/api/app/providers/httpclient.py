"""Shared async JSON HTTP client base for provider implementations.

Centralizes the error taxonomy mapping and the TLS verification policy so
every HTTP provider behaves identically. Subclasses configure base_url,
credentials and headers in __init__ and expose narrow methods only.
"""

import ssl
from typing import Any, Literal

import httpx

from app.providers.base import ProviderStatusValue
from app.providers.errors import ProviderError, ProviderErrorCode
from app.providers.tls_policy import enforce_tls_policy


class BaseJsonClient:
    provider_id: str = ""

    def __init__(self) -> None:
        self.base_url: str = ""
        self.auth: tuple[str, str] | None = None
        self.headers: dict[str, str] = {}
        self.verify_tls: bool = True
        self.timeout_seconds: float = 8.0
        self.trust_env: bool = True

    def is_configured(self) -> bool:
        return bool(self.base_url)

    def has_credentials(self) -> bool:
        return True

    def credentials_error(self) -> str:
        return f"{self.provider_id} credentials are not configured"

    def _check_tls_policy(self) -> None:
        enforce_tls_policy(
            provider_id=self.provider_id,
            base_url=self.base_url,
            verify_tls=self.verify_tls,
        )

    def _error_detail(self, response: httpx.Response) -> str:
        return f"{self.provider_id} returned HTTP {response.status_code}"

    async def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        timeout: float | None = None,
        *,
        response_mode: Literal["json", "text", "auto"] = "json",
        json_body: Any | None = None,
    ) -> Any:
        if not self.is_configured():
            raise ProviderError(
                "configuration_missing", f"{self.provider_id} base_url is not configured"
            )
        if not self.has_credentials():
            raise ProviderError("credentials_missing", self.credentials_error())
        self._check_tls_policy()

        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(
                verify=self.verify_tls,
                timeout=timeout or self.timeout_seconds,
                auth=self.auth,
                headers=self.headers or None,
                trust_env=self.trust_env,
            ) as client:
                response = await client.request(
                    method, url, json=json_body if json_body is not None else None
                )
        except httpx.TimeoutException:
            raise ProviderError(
                "timeout", f"{self.provider_id} did not respond within the timeout ({path})"
            )
        except httpx.ConnectError as exc:
            if isinstance(exc.__cause__, ssl.SSLError) or "ssl" in str(exc).lower():
                raise ProviderError("tls_error", f"TLS handshake with {self.provider_id} failed")
            raise ProviderError("unreachable", f"{self.provider_id} host is unreachable")
        except httpx.HTTPError as exc:
            raise ProviderError(
                "unreachable", f"{self.provider_id} request failed: {exc.__class__.__name__}"
            )

        if response.status_code == 401:
            raise ProviderError("auth_failed", f"{self.provider_id} rejected the credentials")
        if response.status_code == 403:
            raise ProviderError(
                "permission_denied", f"{self.provider_id} credentials lack permission"
            )
        if response.status_code >= 500:
            raise ProviderError(
                "degraded", self._error_detail(response)
            )
        if response.status_code >= 400:
            raise ProviderError(
                "invalid_response", self._error_detail(response)
            )

        if response_mode == "text":
            return response.text
        if response_mode == "auto" and "application/json" not in response.headers.get(
            "content-type", ""
        ):
            return response.text
        try:
            return response.json()
        except ValueError:
            raise ProviderError(
                "invalid_response", f"{self.provider_id} returned a non-JSON response"
            )

    async def get(
        self,
        path: str,
        timeout: float | None = None,
        *,
        response_mode: Literal["json", "text", "auto"] = "json",
    ) -> Any:
        return await self._request("GET", path, timeout, response_mode=response_mode)

    async def post(
        self,
        path: str,
        timeout: float | None = None,
        *,
        response_mode: Literal["json", "text", "auto"] = "json",
        json_body: Any | None = None,
    ) -> Any:
        return await self._request(
            "POST", path, timeout, response_mode=response_mode, json_body=json_body
        )


HEALTH_STATUS_MAP: dict[ProviderErrorCode, ProviderStatusValue] = {
    "timeout": "unreachable",
    "unreachable": "unreachable",
    "tls_error": "misconfigured",
    "auth_failed": "misconfigured",
    "permission_denied": "misconfigured",
    "degraded": "degraded",
    "invalid_response": "degraded",
    "configuration_missing": "misconfigured",
    "credentials_missing": "misconfigured",
}
