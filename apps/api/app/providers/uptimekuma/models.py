"""Normalized Uptime Kuma models. These are the only shapes exposed to the
frontend and to model providers — never the raw vendor response."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class MonitorStatus(_Model):
    name: str = ""
    type: str = ""
    target: str = ""
    status: str = ""


class HeartbeatMonitor(_Model):
    monitor_id: str = ""
    status: str = "unknown"
    last_ping_ms: int | float | None = None
    last_time: str = ""
    uptime_24h: int | float | None = None
