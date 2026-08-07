from pydantic import BaseModel, ConfigDict


class VpsSystemStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str = ""
    os_name: str = ""
    linux_distro: str = ""
    platform: str = ""
    uptime_seconds: int | None = None


class VpsResourceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpu_percent: float | None = None
    load_1m: float | None = None
    load_5m: float | None = None
    load_15m: float | None = None
    memory_percent: float | None = None
    disk_percent_max: float | None = None
    disk_paths_high: list[str] = []


class VpsWireGuardInterface(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    present: bool
    rx_bytes: int | None = None
    tx_bytes: int | None = None


class VpsDeployTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = ""
    url: str
    ok: bool
    status_code: int | None = None
    duration_ms: int | None = None
    error: str = ""


class VpsGlancesStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    source: str = "glances"
    system: VpsSystemStatus
    resources: VpsResourceStatus


class VpsWireGuardStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    source: str = "glances+inventory"
    interfaces: list[VpsWireGuardInterface]
    route_targets: list[dict]
    limitations: list[str] = []


class VpsDeployStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    targets: list[VpsDeployTarget]
    total: int
    unhealthy: int
