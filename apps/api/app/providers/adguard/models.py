"""Normalized AdGuard Home models. These are the only shapes exposed to the
frontend and to model providers — never the raw vendor response."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AdguardStatus(_Model):
    version: str = ""
    running: bool | None = None
    protection_enabled: bool | None = None
    dns_addresses: list[str] = []
    dns_port: int | None = None


class TopEntry(_Model):
    name: str = ""
    count: int | None = None


class AdguardStats(_Model):
    time_units: str = ""
    dns_queries: int | None = None
    blocked_filtering: int | None = None
    replaced_safebrowsing: int | None = None
    replaced_parental: int | None = None
    avg_processing_time_ms: float | None = None
    top_queried_domains: list[TopEntry] = []
    top_blocked_domains: list[TopEntry] = []


class FilterInfo(_Model):
    name: str = ""
    enabled: bool | None = None
    rules_count: int | None = None
    last_updated: str = ""


class FilteringStatus(_Model):
    enabled: bool | None = None
    update_interval_hours: int | None = None
    filters_total: int = 0
    filters: list[FilterInfo] = []
    user_rules_count: int = 0
