"""Normalized ZeroTier models exposed outside the provider boundary."""

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ZeroTierNetwork(_Model):
    id: str
    name: str = ""
    description: str = ""
    private: bool | None = None
    members_total: int | None = None
    members_authorized: int | None = None


class ZeroTierMember(_Model):
    id: str
    network_id: str
    name: str = ""
    authorized: bool = False
    online: bool = False
    stale: bool = True
    last_seen_at: str = ""
    assigned_ips: list[str] = Field(default_factory=list)
