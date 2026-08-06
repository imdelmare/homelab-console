from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ProviderStatusValue = Literal[
    "healthy",
    "degraded",
    "unreachable",
    "unavailable",
    "misconfigured",
    "unknown",
]


class ProviderHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    status: ProviderStatusValue = "unknown"
    detail: str = ""
    checked_at: datetime | None = None
    last_ok_at: datetime | None = None


class Provider(ABC):
    """A provider exposes narrow capabilities against one homelab system."""

    id: str
    display_name: str
    # Secret keys the provider expects in the secrets file (references only).
    credential_requirements: tuple[str, ...] = ()

    @abstractmethod
    def ready(self) -> bool:
        """True when configuration and credentials are present."""

    @abstractmethod
    async def health(self) -> ProviderHealth:
        """Cheap reachability/health probe. Must never raise."""

    def capabilities(self) -> list[str]:
        from app.tools.registry import list_tools

        return [tool.id for tool in list_tools() if tool.provider_id == self.id]
