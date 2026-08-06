"""Normalized OPNsense models. These are the only shapes exposed to the
frontend and to model providers — never the raw vendor response."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class FirmwareStatus(_Model):
    product_version: str = ""
    product_latest: str = ""
    os_version: str = ""
    status: str = ""
    status_message: str = ""
    needs_reboot: bool = False
    new_packages: int = 0
    upgrade_packages: int = 0
    reinstall_packages: int = 0
    last_check: str = ""


class SubsystemStatus(_Model):
    subsystem: str = ""
    status: str = "unknown"
    message: str = ""


class SystemInformation(_Model):
    hostname: str = ""
    versions: list[str] = []
    updated_at: str = ""


class SystemResources(_Model):
    memory_total_bytes: int | float | None = None
    memory_used_bytes: int | float | None = None
    memory_used_percent: float | None = None


class TemperatureSensor(_Model):
    sensor_id: str = ""
    kind: str = "other"
    temperature_c: int | float | None = None


class SystemTemperature(_Model):
    sensors: list[TemperatureSensor] = []
    maximum_temperature_c: int | float | None = None


class InterfaceName(_Model):
    device: str = ""
    name: str = ""


class InterfaceStatistics(_Model):
    device: str = ""
    description: str = ""
    rx_bytes: int | float | None = None
    tx_bytes: int | float | None = None
    rx_packets: int | float | None = None
    tx_packets: int | float | None = None
    rx_errors: int | float | None = None
    tx_errors: int | float | None = None
    collisions: int | float | None = None


class ArpEntry(_Model):
    ip_address: str = ""
    mac_address: str = ""
    hostname: str = ""
    manufacturer: str = ""
    interface: str = ""
    interface_name: str = ""
    type: str = ""
    expires: str = ""


class KeaLease(_Model):
    ip_address: str = ""
    mac_address: str = ""
    hostname: str = ""
    interface: str = ""
    subnet_id: str = ""
    state: str = ""
    starts_at: str = ""
    ends_at: str = ""
    valid_lifetime_seconds: int | float | None = None


class WireguardPeer(_Model):
    name: str = ""
    endpoint: str = ""
    allowed_ips: str = ""
    latest_handshake_at: int | None = None
    handshake_age_seconds: int | None = None
    connected: bool = False
    rx_bytes: int | float | None = None
    tx_bytes: int | float | None = None


class WireguardInterface(_Model):
    device: str = ""
    name: str = ""
    listen_port: int | float | None = None
    peers: list[WireguardPeer] = []


class ServiceInfo(_Model):
    id: str = ""
    name: str = ""
    description: str = ""
    running: bool = False
    locked: bool = False


class GatewayInfo(_Model):
    name: str = ""
    address: str = ""
    status: str = "unknown"
    online: bool = False
    loss_percent: int | float | None = None
    rtt_ms: int | float | None = None
    rtt_stddev_ms: int | float | None = None
