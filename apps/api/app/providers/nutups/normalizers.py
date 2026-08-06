from __future__ import annotations

from typing import Any

from app.providers.nutups.models import UpsDevice, UpsStatus


def normalize_devices(rows: list[dict[str, str]]) -> list[UpsDevice]:
    return [
        UpsDevice(name=str(item.get("name", "")), description=str(item.get("description", "")))
        for item in rows
        if item.get("name")
    ]


def normalize_status(name: str, variables: dict[str, str]) -> UpsStatus:
    flags = str(variables.get("ups.status") or "").split()
    return UpsStatus(
        name=name,
        status=_status_label(flags),
        status_flags=flags,
        model=str(variables.get("ups.model") or ""),
        manufacturer=str(variables.get("ups.mfr") or variables.get("device.mfr") or ""),
        serial=str(variables.get("ups.serial") or variables.get("device.serial") or ""),
        battery_charge_percent=_float(variables.get("battery.charge")),
        battery_runtime_seconds=_float(variables.get("battery.runtime")),
        battery_voltage=_float(variables.get("battery.voltage")),
        input_voltage=_float(variables.get("input.voltage")),
        output_voltage=_float(variables.get("output.voltage")),
        load_percent=_float(variables.get("ups.load")),
        ups_temperature_c=_float(variables.get("ups.temperature")),
        raw_variables_count=len(variables),
    )


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _status_label(flags: list[str]) -> str:
    if not flags:
        return "unknown"
    flag_set = set(flags)
    if "LB" in flag_set:
        return "low_battery"
    if "OB" in flag_set:
        return "on_battery"
    if "RB" in flag_set:
        return "replace_battery"
    if "ALARM" in flag_set:
        return "alarm"
    if "OL" in flag_set:
        return "online"
    return " ".join(flags).lower()
