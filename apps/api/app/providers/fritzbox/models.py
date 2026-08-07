"""Normalized FritzBox models. These are the only shapes exposed to the
frontend and to model providers — never the raw TR-064 response."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DeviceInfo(_Model):
    manufacturer: str = ""
    model: str = ""
    description: str = ""
    serial_number: str = ""
    software_version: str = ""
    hardware_version: str = ""
    uptime_seconds: int | None = None


class WanStatus(_Model):
    access_type: str = ""
    physical_link_status: str = ""
    upstream_max_mbps: float | None = None
    downstream_max_mbps: float | None = None
    bytes_sent: int | None = None
    bytes_received: int | None = None


class WifiRadio(_Model):
    index: int
    enabled: bool | None = None
    status: str = ""
    ssid: str = ""
    channel: int | None = None
    standard: str = ""
    beacon_type: str = ""


class TemperatureSensor(_Model):
    sensor_id: str = ""
    kind: str = "other"
    temperature_c: int | float | None = None


class SystemTemperature(_Model):
    # False when the hardware has no readable sensor (e.g. repeaters whose
    # ecoStat page exists but always returns an empty series).
    supported: bool = True
    sensors: list[TemperatureSensor] = []
    maximum_temperature_c: int | float | None = None
