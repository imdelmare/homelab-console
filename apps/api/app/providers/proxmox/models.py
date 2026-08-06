"""Normalized Proxmox models. These are the only shapes exposed to the
frontend and to model providers — never the raw vendor response."""

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ProxmoxVersion(_Model):
    version: str = ""
    release: str = ""
    repoid: str = ""


class ClusterStatusEntry(_Model):
    id: str = ""
    name: str = ""
    kind: str = ""  # cluster | node
    online: bool | None = None
    quorate: bool | None = None
    nodes: int | None = None


class NodeInfo(_Model):
    node: str
    status: str = "unknown"
    uptime_seconds: int | None = None
    cpu_usage: float | None = None
    max_cpu: int | None = None
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None


class GuestInfo(_Model):
    vmid: int
    name: str = ""
    guest_type: str  # qemu | lxc
    status: str = "unknown"
    node: str = ""
    cpu_usage: float | None = None
    max_cpu: int | None = None
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None
    uptime_seconds: int | None = None
    tags: str = ""


class ProxmoxTopologySnapshot(_Model):
    """Narrow cluster membership and placement snapshot for the topology UI."""

    entries: list[ClusterStatusEntry] = Field(default_factory=list)
    nodes: list[NodeInfo] = Field(default_factory=list)
    guests: list[GuestInfo] = Field(default_factory=list)


class StorageInfo(_Model):
    id: str
    node: str = ""
    storage_type: str = ""
    total_bytes: int | None = None
    used_bytes: int | None = None
    available_bytes: int | None = None
    active: bool | None = None
    shared: bool | None = None


class NodeStatusInfo(_Model):
    node: str
    uptime_seconds: int | None = None
    loadavg: list[float] = []
    cpu_usage: float | None = None
    cpu_count: int | None = None
    cpu_model: str = ""
    kernel_version: str = ""
    memory_total_bytes: int | None = None
    memory_used_bytes: int | None = None
    swap_total_bytes: int | None = None
    swap_used_bytes: int | None = None
    rootfs_total_bytes: int | None = None
    rootfs_used_bytes: int | None = None


class GuestBackupInfo(_Model):
    vmid: int
    backups_count: int = 0
    latest_backup_at: int | None = None  # epoch seconds
    latest_age_days: float | None = None
    latest_size_bytes: int | None = None
    storage: str = ""
    node: str = ""


class FailedTaskInfo(_Model):
    node: str = ""
    task_type: str = ""
    status: str = ""
    started_at: int | None = None
    finished_at: int | None = None
    vmid: str = ""


class DiskTemperature(_Model):
    node: str = ""
    devpath: str = ""
    disk_model: str = ""
    disk_type: str = ""
    temperature_c: int | float | None = None
    health: str = ""
    wearout: int | float | None = None  # as reported by Proxmox (life remaining %)


class DiskTemperatures(_Model):
    disks: list[DiskTemperature] = []
    maximum_temperature_c: int | float | None = None
