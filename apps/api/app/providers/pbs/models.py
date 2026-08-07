from pydantic import BaseModel, ConfigDict


class PbsVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = ""
    release: str = ""
    repoid: str = ""


class PbsDatastore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    total_bytes: int | None = None
    used_bytes: int | None = None
    available_bytes: int | None = None
    used_percent: float | None = None
    error: str = ""


class PbsTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upid: str = ""
    worker_type: str = ""
    worker_id: str = ""
    status: str = ""
    user: str = ""
    started_at: int | None = None
    ended_at: int | None = None
    node: str = ""


class PbsVerifyJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    store: str = ""
    schedule: str = ""
    disabled: bool = False
    next_run: int | None = None
    last_run_status: str = ""


class PbsBackupGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store: str
    backup_type: str
    backup_id: str
    latest_backup_at: int | None = None
    snapshots_count: int = 0
