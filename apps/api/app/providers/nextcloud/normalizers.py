"""Convert raw Nextcloud API payloads into normalized internal models."""

from typing import Any

from app.providers.nextcloud.models import CapabilitiesInfo, NextcloudStatus, ServerInfo


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ocs_data(raw: Any) -> dict[str, Any]:
    return _dict(_dict(_dict(raw).get("ocs")).get("data"))


def normalize_status(raw: Any) -> NextcloudStatus:
    raw = _dict(raw)
    return NextcloudStatus(
        installed=raw.get("installed"),
        maintenance=raw.get("maintenance"),
        needs_db_upgrade=raw.get("needsDbUpgrade"),
        version=str(raw.get("versionstring", "")),
        edition=str(raw.get("edition", "")),
    )


def normalize_capabilities(raw: Any) -> CapabilitiesInfo:
    data = _ocs_data(raw)
    version = _dict(data.get("version"))
    caps = _dict(data.get("capabilities"))
    return CapabilitiesInfo(
        version=str(version.get("string", "")),
        edition=str(version.get("edition", "")),
        apps=sorted(caps.keys()),
    )


def normalize_serverinfo(raw: Any) -> ServerInfo:
    data = _ocs_data(raw)
    nextcloud = _dict(data.get("nextcloud"))
    system = _dict(nextcloud.get("system"))
    storage = _dict(nextcloud.get("storage"))
    active_users = _dict(data.get("activeUsers"))
    cpu_load: list[float] = []
    cpu_load_values = _list(system.get("cpuload"))
    for item in cpu_load_values:
        try:
            cpu_load.append(float(item))
        except (TypeError, ValueError):
            continue
    return ServerInfo(
        version=str(system.get("version", "")),
        freespace_bytes=_int_or_none(system.get("freespace")),
        memory_total_kb=_int_or_none(system.get("mem_total")),
        memory_free_kb=_int_or_none(system.get("mem_free")),
        cpu_load=cpu_load,
        users_total=_int_or_none(storage.get("num_users")),
        files_total=_int_or_none(storage.get("num_files")),
        active_users_last_day=_int_or_none(active_users.get("last24hours")),
    )
