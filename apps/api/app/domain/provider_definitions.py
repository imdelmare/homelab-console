"""Public metadata describing standard provider implementations."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProviderDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    transport: Literal["http_json", "tcp_text"] = "http_json"
    driver_id: str
    configuration_keys: list[str] = Field(default_factory=list)
    capability_tool_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    supports_instances: bool = False
