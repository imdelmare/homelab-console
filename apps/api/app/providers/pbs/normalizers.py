from __future__ import annotations

from typing import Any

from app.providers.pbs.models import PbsBackupGroup, PbsDatastore, PbsTask, PbsVerifyJob, PbsVersion


def normalize_version(raw: dict[str, Any]) -> PbsVersion:
    return PbsVersion(
        version=str(raw.get("version", "")),
        release=str(raw.get("release", "")),
        repoid=str(raw.get("repoid", "")),
    )


def normalize_datastores(raw: list[dict[str, Any]], statuses: dict[str, dict[str, Any]]) -> list[PbsDatastore]:
    rows = []
    for item in raw or []:
        name = str(item.get("store") or item.get("name") or "")
        status = statuses.get(name, {})
        total = _int(status.get("total"))
        used = _int(status.get("used"))
        available = _int(status.get("avail") or status.get("available"))
        rows.append(
            PbsDatastore(
                name=name,
                total_bytes=total,
                used_bytes=used,
                available_bytes=available,
                used_percent=round(used / total * 100, 1) if used is not None and total else None,
                error=str(status.get("error") or ""),
            )
        )
    return rows


def normalize_tasks(raw: list[dict[str, Any]]) -> list[PbsTask]:
    return [
        PbsTask(
            upid=str(item.get("upid", "")),
            worker_type=str(item.get("worker_type") or item.get("type") or ""),
            worker_id=str(item.get("worker_id") or item.get("id") or ""),
            status=str(item.get("status") or ""),
            user=str(item.get("user") or ""),
            started_at=_int(item.get("starttime")),
            ended_at=_int(item.get("endtime")),
            node=str(item.get("node") or ""),
        )
        for item in raw or []
        if isinstance(item, dict)
    ]


def normalize_verify_jobs(raw: list[dict[str, Any]]) -> list[PbsVerifyJob]:
    return [
        PbsVerifyJob(
            id=str(item.get("id") or ""),
            store=str(item.get("store") or ""),
            schedule=str(item.get("schedule") or ""),
            disabled=bool(item.get("disable") or item.get("disabled") or False),
            next_run=_int(item.get("next-run")),
            last_run_status=str(item.get("last-run-state") or item.get("last-run-status") or ""),
        )
        for item in raw or []
        if isinstance(item, dict)
    ]


def normalize_backup_groups(store: str, snapshots: list[dict[str, Any]]) -> list[PbsBackupGroup]:
    grouped: dict[tuple[str, str], PbsBackupGroup] = {}
    for item in snapshots or []:
        backup_type = str(item.get("backup-type") or item.get("backup_type") or "")
        backup_id = str(item.get("backup-id") or item.get("backup_id") or "")
        if not backup_type or not backup_id:
            continue
        key = (backup_type, backup_id)
        backup_time = _int(item.get("backup-time") or item.get("backup_time"))
        current = grouped.get(key)
        if current is None:
            grouped[key] = PbsBackupGroup(
                store=store,
                backup_type=backup_type,
                backup_id=backup_id,
                latest_backup_at=backup_time,
                snapshots_count=1,
            )
        else:
            current.snapshots_count += 1
            if backup_time and (current.latest_backup_at is None or backup_time > current.latest_backup_at):
                current.latest_backup_at = backup_time
    return sorted(grouped.values(), key=lambda item: (item.backup_type, item.backup_id))


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None
