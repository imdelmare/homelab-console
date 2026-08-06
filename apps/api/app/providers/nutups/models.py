from pydantic import BaseModel, ConfigDict


class UpsDevice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    description: str = ""


class UpsStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    status: str = "unknown"
    status_flags: list[str] = []
    model: str = ""
    manufacturer: str = ""
    serial: str = ""
    battery_charge_percent: float | None = None
    battery_runtime_seconds: float | None = None
    battery_voltage: float | None = None
    input_voltage: float | None = None
    output_voltage: float | None = None
    load_percent: float | None = None
    ups_temperature_c: float | None = None
    raw_variables_count: int = 0
