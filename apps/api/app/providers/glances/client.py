"""Read-only client for Glances REST APIs on multiple LAN hosts.

Same telemetry pattern as the VPS Glances endpoint, replicated per host:
plain HTTP on the LAN, no system credentials involved.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from app.providers.errors import ProviderError
from app.services.inventory import provider_config


@dataclass(frozen=True)
class GlancesTarget:
    id: str
    base_url: str


class GlancesHostsClient:
    provider_id = "glances"

    def __init__(self) -> None:
        config = provider_config(self.provider_id)
        self.timeout_seconds = float(config.get("timeout_seconds", 6.0))
        self.targets: list[GlancesTarget] = []
        for item in config.get("hosts") or []:
            if not isinstance(item, dict):
                continue
            host_id = str(item.get("id", ""))
            base_url = str(item.get("base_url", "")).rstrip("/")
            if host_id and base_url:
                self.targets.append(GlancesTarget(id=host_id, base_url=base_url))

    def is_configured(self) -> bool:
        return bool(self.targets)

    async def get(self, target: GlancesTarget, path: str) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(f"{target.base_url}{path}")
        except httpx.TimeoutException:
            raise ProviderError("timeout", f"glances on {target.id} did not respond within the timeout")
        except httpx.HTTPError as exc:
            raise ProviderError("unreachable", f"glances on {target.id} is unreachable: {exc.__class__.__name__}")
        if response.status_code >= 400:
            raise ProviderError("invalid_response", f"glances on {target.id} returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ProviderError("invalid_response", f"glances on {target.id} returned a non-JSON payload")

    async def sensors(self, target: GlancesTarget) -> Any:
        return await self.get(target, "/api/4/sensors")
