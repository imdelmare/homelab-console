"""Convert raw OPNsense API payloads into normalized internal models.

Numeric values arriving as annotated strings ("12.3 ms", "0.0 %") are
converted to plain numbers; unknown or extra vendor fields are dropped."""

import re
import time
from typing import Any

from app.providers.opnsense.models import (
    ArpEntry,
    FirmwareStatus,
    GatewayInfo,
    InterfaceName,
    InterfaceStatistics,
    KeaLease,
    ServiceInfo,
    SubsystemStatus,
    SystemInformation,
    SystemResources,
    SystemTemperature,
    TemperatureSensor,
    WireguardInterface,
    WireguardPeer,
)

_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")

# A WireGuard peer is considered connected when the last handshake is within
# this window (keepalive default is 25s; 3+ minutes means the peer is gone).
WIREGUARD_HANDSHAKE_FRESH_SECONDS = 190


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | int | None:
    """Extract a number from vendor values like '12.3 ms', '0.0 %', '1024'."""
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    match = _NUMBER.search(value.replace(",", "."))
    if not match:
        return None
    text = match.group(0)
    return float(text) if "." in text else int(text)


def _first(entry: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in entry:
            return entry[key]
    return None


def _truthy_flag(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes")


def normalize_firmware(raw: Any) -> FirmwareStatus:
    raw = _dict(raw)
    product = _dict(raw.get("product"))
    return FirmwareStatus(
        product_version=str(raw.get("product_version") or product.get("product_version") or ""),
        product_latest=str(product.get("product_latest") or ""),
        os_version=str(raw.get("os_version") or ""),
        status=str(raw.get("status") or ""),
        status_message=str(raw.get("status_msg") or "")[:300],
        needs_reboot=_truthy_flag(raw.get("needs_reboot")),
        new_packages=len(raw.get("new_packages") or []),
        upgrade_packages=len(raw.get("upgrade_packages") or []),
        reinstall_packages=len(raw.get("reinstall_packages") or []),
        last_check=str(raw.get("last_check") or ""),
    )


def normalize_subsystems(raw: Any) -> list[SubsystemStatus]:
    subsystems = []
    for name, info in sorted(_dict(raw).items()):
        entry = _dict(info)
        if not entry:
            continue
        status = entry.get("status")
        if status is None and "statusCode" in entry:
            status = entry.get("statusCode")
        subsystems.append(
            SubsystemStatus(
                subsystem=str(name),
                status=str(status if status is not None else "unknown"),
                message=str(entry.get("message") or "")[:300],
            )
        )
    return subsystems


def normalize_system_information(raw: Any) -> SystemInformation:
    raw = _dict(raw)
    versions = raw.get("versions")
    return SystemInformation(
        hostname=str(raw.get("name") or ""),
        versions=[str(item) for item in versions] if isinstance(versions, list) else [],
        updated_at=str(raw.get("date") or ""),
    )


def normalize_system_resources(raw: Any) -> SystemResources:
    memory = _dict(_dict(raw).get("memory"))
    total = _number(memory.get("total"))
    used = _number(memory.get("used"))
    percent = None
    if total and used is not None:
        percent = round(used / total * 100, 1)
    return SystemResources(
        memory_total_bytes=total,
        memory_used_bytes=used,
        memory_used_percent=percent,
    )


def normalize_system_temperature(raw: Any) -> SystemTemperature:
    if isinstance(raw, list):
        rows = raw
    else:
        rows = _dict(raw).get("rows") or _dict(raw).get("items") or _dict(raw).get("temperatures")
    if not isinstance(rows, list):
        rows = []

    sensors = []
    for row in rows:
        entry = _dict(row)
        device = str(entry.get("device") or "").strip()
        sequence = str(entry.get("device_seq") or "").strip()
        temperature_c = _number(entry.get("temperature"))
        if not device or temperature_c is None:
            continue
        sensor_id = f"{device}.{sequence}" if sequence else device
        sensors.append(
            TemperatureSensor(
                sensor_id=sensor_id,
                kind=str(entry.get("type") or "other"),
                temperature_c=temperature_c,
            )
        )

    temperatures = [
        sensor.temperature_c
        for sensor in sensors
        if isinstance(sensor.temperature_c, (int, float))
    ]
    return SystemTemperature(
        sensors=sensors,
        maximum_temperature_c=max(temperatures) if temperatures else None,
    )


def normalize_interface_names(raw: Any) -> list[InterfaceName]:
    return [
        InterfaceName(device=str(device), name=str(name))
        for device, name in sorted(_dict(raw).items())
        if isinstance(name, str)
    ]


def normalize_interface_stats(device: str, entry: dict[str, Any]) -> InterfaceStatistics:
    return InterfaceStatistics(
        device=str(entry.get("name") or device),
        description=str(entry.get("description") or ""),
        rx_bytes=_number(_first(entry, "bytes received", "rx-bytes", "rx_bytes")),
        tx_bytes=_number(_first(entry, "bytes transmitted", "tx-bytes", "tx_bytes")),
        rx_packets=_number(_first(entry, "packets received", "rx-packets", "rx_packets")),
        tx_packets=_number(_first(entry, "packets transmitted", "tx-packets", "tx_packets")),
        rx_errors=_number(_first(entry, "input errors", "rx-errors", "rx_errors")),
        tx_errors=_number(_first(entry, "output errors", "tx-errors", "tx_errors")),
        collisions=_number(entry.get("collisions")),
    )


def normalize_interface_statistics(raw: Any) -> list[InterfaceStatistics]:
    raw = _dict(raw)
    # Some versions nest the mapping under "interfaces" or "statistics".
    mapping = _dict(raw.get("interfaces")) or _dict(raw.get("statistics")) or raw
    return [
        normalize_interface_stats(device, entry)
        for device, entry in sorted(mapping.items())
        if isinstance(entry, dict)
    ]


def normalize_arp_entries(raw: Any) -> list[ArpEntry]:
    # /api/diagnostics/interface/get_arp responds with a bare JSON array,
    # unlike every other OPNsense endpoint this module normalizes (which
    # wrap rows in a dict). Handle both shapes rather than silently
    # discarding real data via _dict()'s any-non-dict-becomes-{} behavior.
    if isinstance(raw, list):
        rows = raw
    else:
        rows = _dict(raw).get("rows") or _dict(raw).get("items") or _dict(raw).get("arp")
    if not isinstance(rows, list):
        rows = []
    entries = []
    for row in rows:
        entry = _dict(row)
        ip_address = str(_first(entry, "ip", "ip-address", "ip_address", "address") or "")
        mac_address = str(_first(entry, "mac", "mac-address", "mac_address", "macaddr") or "")
        if not ip_address and not mac_address:
            continue
        entries.append(
            ArpEntry(
                ip_address=ip_address,
                mac_address=mac_address,
                hostname=str(_first(entry, "hostname", "host") or ""),
                manufacturer=str(_first(entry, "manufacturer", "vendor") or ""),
                interface=str(_first(entry, "interface", "if", "intf") or ""),
                interface_name=str(_first(entry, "interface_name", "interface-name", "if_descr", "intf_description") or ""),
                type=str(_first(entry, "type", "entry_type") or ""),
                expires=str(_first(entry, "expires", "expire", "expiration") or ""),
            )
        )
    return entries


def normalize_kea_leases(raw: Any) -> list[KeaLease]:
    raw = _dict(raw)
    rows = raw.get("rows") or raw.get("items") or raw.get("leases") or raw.get("data")
    if not isinstance(rows, list):
        rows = []
    leases = []
    for row in rows:
        entry = _dict(row)
        ip_address = str(_first(entry, "address", "ip", "ip_address", "ip-address") or "")
        mac_address = str(_first(entry, "hwaddr", "mac", "mac_address", "mac-address", "duid") or "")
        if not ip_address and not mac_address:
            continue
        raw_state = _first(entry, "state", "cltt_state", "status")
        state = str(raw_state if raw_state is not None else "")
        if raw_state == 0 or state == "0":
            state = "active"
        elif raw_state == 1 or state == "1":
            state = "inactive"
        leases.append(
            KeaLease(
                ip_address=ip_address,
                mac_address=mac_address,
                hostname=str(_first(entry, "hostname", "client_hostname", "client-hostname", "host") or ""),
                interface=str(_first(entry, "if_descr", "if_name", "if", "interface", "interface_name", "interface-name") or ""),
                subnet_id=str(_first(entry, "subnet_id", "subnet-id", "subnet") or ""),
                state=state,
                starts_at=str(_first(entry, "starts", "starts_at", "start", "cltt") or ""),
                ends_at=str(_first(entry, "ends", "ends_at", "expire", "expires", "valid_until") or ""),
                valid_lifetime_seconds=_number(_first(entry, "valid_lifetime", "valid-lifetime", "valid_lifetime_seconds")),
            )
        )
    return leases


def normalize_wireguard(raw: Any) -> tuple[list[WireguardInterface], list[WireguardPeer]]:
    """Group WireGuard peers under their interfaces; peers whose interface is
    unknown are returned separately as orphans."""
    raw = _dict(raw)
    rows = raw.get("rows") if isinstance(raw.get("rows"), list) else []
    now = time.time()

    interfaces: dict[str, WireguardInterface] = {}
    orphan_peers: list[WireguardPeer] = []
    for row in rows:
        entry = _dict(row)
        device = str(entry.get("if") or "")
        if entry.get("type") == "interface":
            interfaces[device] = WireguardInterface(
                device=device,
                name=str(entry.get("name") or ""),
                listen_port=_number(entry.get("port")),
            )
        elif entry.get("type") == "peer":
            handshake_epoch = _number(entry.get("latest-handshake")) or 0
            age = now - handshake_epoch if handshake_epoch else None
            peer = WireguardPeer(
                name=str(entry.get("name") or ""),
                endpoint=str(entry.get("endpoint") or ""),
                allowed_ips=str(entry.get("allowed-ips") or ""),
                latest_handshake_at=int(handshake_epoch) or None,
                handshake_age_seconds=int(age) if age is not None else None,
                connected=age is not None and age < WIREGUARD_HANDSHAKE_FRESH_SECONDS,
                rx_bytes=_number(entry.get("transfer-rx")),
                tx_bytes=_number(entry.get("transfer-tx")),
            )
            if device in interfaces:
                interfaces[device].peers.append(peer)
            else:
                orphan_peers.append(peer)
    return list(interfaces.values()), orphan_peers


def normalize_services(raw: Any) -> list[ServiceInfo]:
    raw = _dict(raw)
    rows = raw.get("rows") if isinstance(raw.get("rows"), list) else []
    services = []
    for row in rows:
        entry = _dict(row)
        if not entry.get("name") and not entry.get("id"):
            continue
        services.append(
            ServiceInfo(
                id=str(entry.get("id") or entry.get("name") or ""),
                name=str(entry.get("name") or ""),
                description=str(entry.get("description") or ""),
                running=_truthy_flag(entry.get("running")),
                locked=_truthy_flag(entry.get("locked")),
            )
        )
    return services


def normalize_gateway(item: dict[str, Any]) -> GatewayInfo:
    status = str(item.get("status_translated") or item.get("status") or "unknown")
    return GatewayInfo(
        name=str(item.get("name") or ""),
        address=str(item.get("address") or ""),
        status=status,
        online=status.strip().lower() in ("online", "none"),
        loss_percent=_number(item.get("loss")),
        rtt_ms=_number(item.get("delay")),
        rtt_stddev_ms=_number(item.get("stddev")),
    )


def normalize_gateways(raw: Any) -> list[GatewayInfo]:
    items = _dict(raw).get("items")
    return [
        normalize_gateway(item)
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict)
    ]
