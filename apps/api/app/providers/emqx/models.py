"""Normalized EMQX models. These are the only shapes exposed to the
frontend and to model providers — never the raw vendor response."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmqxNode(_Model):
    node: str = ""
    status: str = ""
    version: str = ""
    uptime_ms: int | None = None
    connections: int | None = None
    memory_used: int | str | None = None
    memory_total: int | str | None = None
    load1: int | float | str | None = None


class EmqxStats(_Model):
    connections: int | float | None = None
    connections_max: int | float | None = None
    live_connections: int | float | None = None
    sessions: int | float | None = None
    subscriptions: int | float | None = None
    topics: int | float | None = None
    retained_messages: int | float | None = None
