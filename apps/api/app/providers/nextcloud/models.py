"""Normalized Nextcloud models. These are the only shapes exposed to the
frontend and to model providers — never the raw vendor response."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class NextcloudStatus(_Model):
    installed: bool | None = None
    maintenance: bool | None = None
    needs_db_upgrade: bool | None = None
    version: str = ""
    edition: str = ""


class CapabilitiesInfo(_Model):
    version: str = ""
    edition: str = ""
    apps: list[str] = []


class ServerInfo(_Model):
    version: str = ""
    freespace_bytes: int | None = None
    memory_total_kb: int | None = None
    memory_free_kb: int | None = None
    cpu_load: list[float] = []
    users_total: int | None = None
    files_total: int | None = None
    active_users_last_day: int | None = None
