"""Normalized Cloudflare Tunnel API models."""

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TunnelStatus(_Model):
    id: str
    name: str = ""
    status: str = "unknown"
    reported_status: str = ""
    config_source: str = ""
    connected_at: str = ""
    disconnected_at: str = ""


class ConnectorStatus(_Model):
    tunnel_id: str
    version: str = ""
    architecture: str = ""
    run_at: str = ""
    features: list[str] = Field(default_factory=list)
    connections_total: int = 0
    connections_active: int = 0
    connections_pending_reconnect: int = 0
