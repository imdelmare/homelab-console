from __future__ import annotations

from typing import Any

from app.providers.errors import ProviderError
from app.providers.httpclient import BaseJsonClient
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class VpsGlancesClient(BaseJsonClient):
    provider_id = "vps"

    def __init__(self) -> None:
        super().__init__()
        secrets = get_provider_secrets("vps")
        config = provider_config("vps")
        env = load_credentials_env()

        glances = _dict(config.get("glances"))
        secret_glances = _dict(secrets.get("glances"))
        self.base_url = str(
            glances.get("base_url")
            or secret_glances.get("base_url")
            or secrets.get("glances_base_url")
            or env.get("VPS_GLANCES_URL")
            or ""
        ).rstrip("/")
        token = secret_glances.get("token") or secrets.get("glances_token") or env.get("VPS_GLANCES_TOKEN") or ""
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.verify_tls = bool(glances.get("verify_tls", secret_glances.get("verify_tls", True)))
        self.timeout_seconds = float(glances.get("timeout_seconds", secret_glances.get("timeout_seconds", 6.0)))

    async def all(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            raw = await self.get("/api/4/all", timeout=timeout)
        except ProviderError as exc:
            if exc.code == "unreachable":
                raise ProviderError("unreachable", "vps glances API is unreachable")
            raise
        return raw if isinstance(raw, dict) else {}
