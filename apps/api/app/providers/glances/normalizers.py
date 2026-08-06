"""Convert raw Glances /api/4/sensors payloads into normalized models."""

from typing import Any

from app.providers.glances.models import HostTemperatures, TemperatureSensor

_CPU_MARKERS = ("core", "package", "cpu", "k10temp", "x86_pkg", "acpitz", "soc")


def _kind(label: str, sensor_type: str) -> str:
    normalized = f"{label} {sensor_type}".lower()
    if "temperature_hdd" in normalized or "nvme" in normalized or "drivetemp" in normalized:
        return "disk"
    if any(marker in normalized for marker in _CPU_MARKERS):
        return "cpu"
    return "other"


def normalize_host_sensors(host_id: str, address: str, raw: Any) -> HostTemperatures:
    sensors: list[TemperatureSensor] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        sensor_type = str(item.get("type", ""))
        if item.get("unit") != "C" and not sensor_type.startswith("temperature"):
            continue
        value = item.get("value")
        if not isinstance(value, (int, float)):
            continue
        label = str(item.get("label", "")).strip()
        sensors.append(
            TemperatureSensor(
                sensor_id=label.lower().replace(" ", "_") or "temperature",
                kind=_kind(label, sensor_type),
                temperature_c=value,
            )
        )

    temperatures = [item.temperature_c for item in sensors if item.temperature_c is not None]
    return HostTemperatures(
        host_id=host_id,
        address=address,
        sensors=sensors,
        maximum_temperature_c=max(temperatures) if temperatures else None,
    )
