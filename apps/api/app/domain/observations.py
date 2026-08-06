"""Canonical status observations for individual provider capabilities."""

from datetime import datetime
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from app.providers.base import ProviderStatusValue

ObservationScalar: TypeAlias = str | int | float | bool | None


class CapabilityObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider_id: str
    capability_id: str
    label: str
    tool_id: str
    status: ProviderStatusValue = "unknown"
    detail: str = ""
    checked_at: datetime
    error_code: str = ""
    summary: dict[str, ObservationScalar] = Field(default_factory=dict)
