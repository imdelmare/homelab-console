"""Convert raw FritzBox TR-064 values into normalized internal models."""

from typing import Any

from app.providers.fritzbox.models import DeviceInfo, SystemTemperature, TemperatureSensor, WanStatus, WifiRadio


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "disabled"}:
            return False
    return None


def _bitrate_mbps(value: Any) -> float | None:
    amount = _int(value)
    return round(amount / 1_000_000, 2) if amount is not None else None


def normalize_device_info(raw: dict[str, Any]) -> DeviceInfo:
    return DeviceInfo(
        manufacturer=raw.get("manufacturerName", ""),
        model=raw.get("modelName", ""),
        description=raw.get("description", ""),
        serial_number=raw.get("serialNumber", ""),
        software_version=raw.get("softwareVersion", ""),
        hardware_version=raw.get("hardwareVersion", ""),
        uptime_seconds=_int(raw.get("upTime")),
    )


def normalize_wan(
    properties: dict[str, Any], sent: dict[str, Any], received: dict[str, Any]
) -> WanStatus:
    return WanStatus(
        access_type=properties.get("wanAccessType", ""),
        physical_link_status=properties.get("physicalLinkStatus", ""),
        upstream_max_mbps=_bitrate_mbps(properties.get("layer1UpstreamMaxBitRate")),
        downstream_max_mbps=_bitrate_mbps(properties.get("layer1DownstreamMaxBitRate")),
        bytes_sent=_int(sent.get("totalBytesSent")),
        bytes_received=_int(received.get("totalBytesReceived")),
    )


def normalize_wifi_radio(index: int, raw: dict[str, Any]) -> WifiRadio:
    return WifiRadio(
        index=index,
        enabled=_bool(raw.get("enable")),
        status=raw.get("status", ""),
        ssid=raw.get("ssid", ""),
        channel=_int(raw.get("channel")),
        standard=raw.get("standard", ""),
        beacon_type=raw.get("beaconType", ""),
    )


def normalize_ecostat_temperature(raw: Any) -> SystemTemperature:
    """Project data.lua page=ecoStat into the shared temperature shape.

    cputemp carries series of oldest-first sample strings (the time axis
    labels grow left to right); only the newest sample of each series is
    exposed.
    """
    data = raw.get("data") if isinstance(raw, dict) else {}
    cputemp = data.get("cputemp") if isinstance(data, dict) else {}
    series = cputemp.get("series") if isinstance(cputemp, dict) else []

    sensors = []
    for index, samples in enumerate(series if isinstance(series, list) else []):
        if not isinstance(samples, list) or not samples:
            continue
        try:
            latest = float(str(samples[-1]))
        except ValueError:
            continue
        sensors.append(
            TemperatureSensor(
                sensor_id="cpu" if index == 0 else f"cpu_{index}",
                kind="cpu",
                temperature_c=int(latest) if latest.is_integer() else latest,
            )
        )

    temperatures = [item.temperature_c for item in sensors if item.temperature_c is not None]
    return SystemTemperature(
        supported=bool(sensors),
        sensors=sensors,
        maximum_temperature_c=max(temperatures) if temperatures else None,
    )
