"""Normalized MikroTik models. These are the only shapes exposed to the
frontend and to model providers — never the raw vendor response."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SystemResource(_Model):
    version: str = ""
    board_name: str = ""
    architecture: str = ""
    uptime: str = ""
    cpu_load_percent: int | str | None = None
    memory_free_bytes: int | str | None = None
    memory_total_bytes: int | str | None = None
    disk_free_bytes: int | str | None = None
    disk_total_bytes: int | str | None = None


class TemperatureSensor(_Model):
    sensor_id: str = ""
    kind: str = "other"
    temperature_c: float | int | None = None


class SystemHealth(_Model):
    temperature_sensors: list[TemperatureSensor] = []
    maximum_temperature_c: float | int | None = None
    voltage_v: float | int | None = None


class InterfaceInfo(_Model):
    name: str = ""
    type: str = ""
    running: bool = False
    disabled: bool = False
    mac_address: str = ""
    rx_bytes: int | str | None = None
    tx_bytes: int | str | None = None
    last_link_up: str = ""
