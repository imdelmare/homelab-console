"""Convert raw MikroTik REST/API payloads into normalized internal models."""

from typing import Any

from app.providers.mikrotik.models import InterfaceInfo, SystemHealth, SystemResource, TemperatureSensor


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | int | None:
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", ".")
    if not text:
        return None
    for suffix in (" C", "C", " V", "V"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _sensor_kind(sensor_id: str) -> str:
    normalized = sensor_id.lower()
    if "cpu" in normalized:
        return "cpu"
    if "board" in normalized or "pcb" in normalized:
        return "board"
    if "sfp" in normalized:
        return "module"
    return "other"


def normalize_resource(raw: Any) -> SystemResource:
    raw = _dict(raw)
    return SystemResource(
        version=raw.get("version", ""),
        board_name=raw.get("board-name", ""),
        architecture=raw.get("architecture-name", ""),
        uptime=raw.get("uptime", ""),
        cpu_load_percent=raw.get("cpu-load"),
        memory_free_bytes=raw.get("free-memory"),
        memory_total_bytes=raw.get("total-memory"),
        disk_free_bytes=raw.get("free-hdd-space"),
        disk_total_bytes=raw.get("total-hdd-space"),
    )


def normalize_health(raw: Any) -> SystemHealth:
    sensors: list[TemperatureSensor] = []
    voltage_v: float | int | None = None

    raw_rows = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else [_dict(raw)]

    # RouterOS 7 emits {"name": ..., "value": ...} rows; RouterOS 6 (REST or API
    # socket) emits flat attribute rows like {"temperature": "51", "voltage": "24.1"}.
    rows: list[dict[str, Any]] = []
    for item in raw_rows:
        if "name" in item or "value" in item:
            rows.append(item)
        else:
            rows.extend({"name": key, "value": value} for key, value in item.items())

    for row in rows:
        name = str(row.get("name") or row.get("id") or row.get("type") or "").strip()
        value = _number(row.get("value") if "value" in row else row.get("temperature"))
        unit = str(row.get("type") or row.get("unit") or "").strip().lower()
        lower_name = name.lower()

        if value is None:
            continue
        if "voltage" in lower_name or unit in ("v", "volt"):
            voltage_v = value
            continue
        if "temperature" not in lower_name and "temp" not in lower_name and unit not in ("c", "celsius"):
            continue

        sensor_id = (
            lower_name
            .replace(" ", "_")
            .replace("-", "_")
            .removesuffix("_temperature")
            .removesuffix("_temp")
            or "temperature"
        )
        sensors.append(
            TemperatureSensor(
                sensor_id=sensor_id,
                kind=_sensor_kind(sensor_id),
                temperature_c=value,
            )
        )

    temperatures = [
        sensor.temperature_c
        for sensor in sensors
        if isinstance(sensor.temperature_c, (int, float))
    ]
    return SystemHealth(
        temperature_sensors=sensors,
        maximum_temperature_c=max(temperatures) if temperatures else None,
        voltage_v=voltage_v,
    )


def normalize_interface(item: dict[str, Any]) -> InterfaceInfo:
    return InterfaceInfo(
        name=item.get("name", ""),
        type=item.get("type", ""),
        running=item.get("running") in (True, "true"),
        disabled=item.get("disabled") in (True, "true"),
        mac_address=item.get("mac-address", ""),
        rx_bytes=item.get("rx-byte"),
        tx_bytes=item.get("tx-byte"),
        last_link_up=item.get("last-link-up-time", ""),
    )


def normalize_interfaces(raw: Any) -> list[InterfaceInfo]:
    return [
        normalize_interface(item)
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, dict)
    ]
