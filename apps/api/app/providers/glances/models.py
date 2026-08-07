"""Normalized Glances host sensor models. These are the only shapes exposed
to the frontend and to model providers — never the raw Glances payload."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TemperatureSensor(_Model):
    sensor_id: str = ""
    kind: str = "other"
    temperature_c: int | float | None = None


class HostTemperatures(_Model):
    host_id: str = ""
    address: str = ""
    sensors: list[TemperatureSensor] = []
    maximum_temperature_c: int | float | None = None
    # Per-host failures degrade to an error string so one unreachable host
    # never hides the other hosts' readings.
    error: str | None = None
