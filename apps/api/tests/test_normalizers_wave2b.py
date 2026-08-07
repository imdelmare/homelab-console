"""Normalizer tests for the wave 2b providers (opnsense, emqx, mikrotik,
uptimekuma, asterisk, fritzbox, cloudflaretunnel): fake vendor payloads in,
normalized models out, extra vendor fields dropped."""

import time

from app.providers.asterisk import normalizers as asterisk
from app.providers.cloudflaretunnel import normalizers as cloudflaretunnel
from app.providers.emqx import normalizers as emqx
from app.providers.fritzbox import normalizers as fritzbox
from app.providers.mikrotik import normalizers as mikrotik
from app.providers.opnsense import normalizers as opnsense
from app.providers.uptimekuma import normalizers as uptimekuma


# --- OPNsense ---------------------------------------------------------------


def test_opnsense_firmware_normalized():
    firmware = opnsense.normalize_firmware({
        "status": "update", "status_msg": "3 updates available.",
        "needs_reboot": "1", "os_version": "FreeBSD 14.1-RELEASE-p6",
        "product": {"product_version": "24.7.5", "product_latest": "24.7.7"},
        "upgrade_packages": [{"name": "a"}, {"name": "b"}],
        "new_packages": None, "last_check": "Sun Jul 13",
        "download_size": "12.3MiB",  # vendor extra, must be dropped
    })
    assert firmware.product_version == "24.7.5"
    assert firmware.product_latest == "24.7.7"
    assert firmware.needs_reboot is True
    assert firmware.upgrade_packages == 2
    assert firmware.new_packages == 0
    assert "download_size" not in firmware.model_dump()


def test_opnsense_subsystems_status_code_fallback():
    subsystems = opnsense.normalize_subsystems({
        "CrashReporter": {"statusCode": 2, "message": "No problems."},
        "Firewall": {"status": "OK", "age": "n/a"},
        "Empty": {},
    })
    by_name = {entry.subsystem: entry for entry in subsystems}
    assert len(subsystems) == 2
    assert by_name["CrashReporter"].status == "2"
    assert by_name["Firewall"].status == "OK"
    assert "age" not in by_name["Firewall"].model_dump()


def test_opnsense_resources_annotated_numbers():
    resources = opnsense.normalize_system_resources({
        "memory": {"total": "8589934592", "used": "2147483648", "used_frmt": "2 GiB"},
    })
    assert resources.memory_total_bytes == 8589934592
    assert resources.memory_used_bytes == 2147483648
    assert resources.memory_used_percent == 25.0


def test_opnsense_temperature_normalized():
    temperature = opnsense.normalize_system_temperature([
        {
            "device": "dev.cpu.0.temperature",
            "device_seq": "0",
            "temperature": "48.1 C",
            "type": "cpu",
            "type_translated": "CPU",
            "raw_vendor_field": "drop me",
        },
        {
            "device": "hw.acpi.thermal.tz0.temperature",
            "device_seq": "",
            "temperature": "42.0",
            "type": "zone",
        },
    ])
    assert temperature.maximum_temperature_c == 48.1
    assert temperature.sensors[0].sensor_id == "dev.cpu.0.temperature.0"
    assert temperature.sensors[0].kind == "cpu"
    assert "type_translated" not in temperature.sensors[0].model_dump()


def test_opnsense_interface_statistics_nested_and_flat():
    payload = {
        "igb0": {"name": "igb0", "flags": "8863", "mtu": 1500,
                 "bytes received": "123", "bytes transmitted": "456",
                 "input errors": "0", "output errors": "1", "collisions": "0"},
    }
    for raw in (payload, {"interfaces": payload}, {"statistics": payload}):
        stats = opnsense.normalize_interface_statistics(raw)
        assert len(stats) == 1
        assert stats[0].device == "igb0"
        assert stats[0].rx_bytes == 123
        assert stats[0].tx_errors == 1
        assert "mtu" not in stats[0].model_dump()


def test_opnsense_wireguard_grouping_and_staleness():
    now = int(time.time())
    interfaces, orphans = opnsense.normalize_wireguard({"rows": [
        {"type": "interface", "if": "wg0", "name": "vps-link", "port": "51820",
         "public-key": "IFACEPUBKEY="},
        {"type": "peer", "if": "wg0", "name": "vps", "endpoint": "203.0.113.7:51820",
         "public-key": "PEERPUBKEY=", "latest-handshake": now - 30,
         "transfer-rx": 1000, "transfer-tx": 2000, "allowed-ips": "10.6.0.2/32"},
        {"type": "peer", "if": "wg9", "name": "ghost", "endpoint": "(none)",
         "latest-handshake": now - 7200},
    ]})
    assert len(interfaces) == 1
    assert interfaces[0].device == "wg0"
    assert interfaces[0].listen_port == 51820
    peer = interfaces[0].peers[0]
    assert peer.connected is True
    assert peer.handshake_age_seconds is not None
    assert peer.handshake_age_seconds <= 40
    assert orphans[0].name == "ghost"
    assert orphans[0].connected is False
    # Public keys are not part of the normalized output.
    assert "PEERPUBKEY" not in str([item.model_dump() for item in interfaces])


def test_opnsense_services_and_gateways():
    services = opnsense.normalize_services({"rows": [
        {"id": "unbound", "name": "unbound", "description": "DNS", "running": 1, "locked": 0},
        {"description": "no id, skipped"},
    ]})
    assert len(services) == 1
    assert services[0].running is True

    gateways = opnsense.normalize_gateways({"items": [
        {"name": "WAN_GW", "address": "192.0.2.1", "status": "none",
         "status_translated": "Online", "loss": "0.0 %", "delay": "12.3 ms",
         "stddev": "1.2 ms", "monitor": "192.0.2.1"},
        {"name": "LTE_GW", "status_translated": "Offline", "delay": "~"},
    ]})
    assert gateways[0].online is True
    assert gateways[0].rtt_ms == 12.3
    assert gateways[1].online is False
    assert gateways[1].rtt_ms is None
    assert "monitor" not in gateways[0].model_dump()


# --- EMQX -------------------------------------------------------------------


def test_emqx_nodes_normalized():
    nodes = emqx.normalize_nodes([
        {"node": "emqx@127.0.0.1", "node_status": "running", "version": "5.6.0",
         "uptime": 123456, "connections": 12, "memory_used": 100,
         "memory_total": 200, "load1": 0.2, "otp_release": "26/14.2"},
        "not-a-dict",
    ])
    assert len(nodes) == 1
    assert nodes[0].status == "running"
    assert nodes[0].uptime_ms == 123456
    assert "otp_release" not in nodes[0].model_dump()


def test_emqx_stats_merged_across_nodes():
    stats = emqx.normalize_stats([
        {"connections.count": 5, "topics.count": 4, "retained.count": 1, "noise": "x"},
        {"connections.count": 3, "topics.count": 2},
    ])
    assert stats.connections == 8
    assert stats.topics == 6
    assert stats.retained_messages == 1
    assert stats.sessions is None
    assert "noise" not in stats.model_dump()


# --- MikroTik ---------------------------------------------------------------


def test_mikrotik_resource_normalized():
    resource = mikrotik.normalize_resource({
        "version": "7.15.2 (stable)", "board-name": "hAP ac3",
        "architecture-name": "arm", "uptime": "2w3d", "cpu-load": 4,
        "free-memory": 100, "total-memory": 200,
        "free-hdd-space": 50, "total-hdd-space": 128,
        "cpu-frequency": 448,  # vendor extra, must be dropped
    })
    assert resource.board_name == "hAP ac3"
    assert resource.cpu_load_percent == 4
    assert resource.disk_total_bytes == 128
    assert "cpu-frequency" not in resource.model_dump()


def test_mikrotik_health_rest_v7_normalized():
    health = mikrotik.normalize_health([
        {"name": "temperature", "value": "41", "type": "C"},
        {"name": "cpu-temperature", "value": "48.5", "type": "C"},
        {"name": "voltage", "value": "24.2", "type": "V"},
        {"name": "fan-speed", "value": "0", "type": "RPM"},
    ])
    assert health.maximum_temperature_c == 48.5
    assert health.voltage_v == 24.2
    assert [sensor.sensor_id for sensor in health.temperature_sensors] == ["temperature", "cpu"]
    assert health.temperature_sensors[1].kind == "cpu"


def test_mikrotik_health_legacy_dict_normalized():
    health = mikrotik.normalize_health({
        "board-temperature": "39C",
        "cpu-temperature": "51",
        "voltage": "12.1V",
    })
    assert health.maximum_temperature_c == 51
    assert health.voltage_v == 12.1
    assert {sensor.kind for sensor in health.temperature_sensors} == {"board", "cpu"}


def test_mikrotik_health_legacy_api_socket_row_normalized():
    # RouterOS 6 over the API socket returns one !re row with flat attributes
    # (observed on the wAP R, 6.49.18), not {"name": ..., "value": ...} rows.
    health = mikrotik.normalize_health([
        {"voltage": "24.1", "temperature": "51"},
    ])
    assert health.maximum_temperature_c == 51
    assert health.voltage_v == 24.1
    assert [sensor.sensor_id for sensor in health.temperature_sensors] == ["temperature"]


def test_mikrotik_interfaces_flags_coerced():
    interfaces = mikrotik.normalize_interfaces([
        {"name": "ether1", "type": "ether", "running": "true", "disabled": "false",
         "mac-address": "AA:BB:CC:00:11:22", "rx-byte": "123", "tx-byte": "456",
         ".id": "*1"},
        {"name": "lte1", "type": "lte", "running": "false"},
        "not-a-dict",
    ])
    assert len(interfaces) == 2
    assert interfaces[0].running is True
    assert interfaces[0].rx_bytes == "123"
    assert interfaces[1].running is False
    assert ".id" not in interfaces[0].model_dump()


# --- Uptime Kuma ------------------------------------------------------------


def test_uptimekuma_metrics_parsed():
    monitors = uptimekuma.parse_monitor_metrics(
        '# HELP monitor_status Monitor Status\n'
        'monitor_status{monitor_name="proxmox",monitor_type="http",monitor_url="https://pve"} 1\n'
        'monitor_status{monitor_name="frigate",monitor_type="http",monitor_url=""} 0\n'
        'monitor_status{monitor_name="backup",monitor_type="ping",monitor_url=""} 3\n'
        'monitor_response_time{monitor_name="proxmox"} 42\n'
    )
    assert [(monitor.name, monitor.status) for monitor in monitors] == [
        ("proxmox", "up"), ("frigate", "down"), ("backup", "maintenance"),
    ]
    assert "monitor_url" not in monitors[0].model_dump()


def test_uptimekuma_heartbeats_normalized():
    monitors = uptimekuma.normalize_heartbeats({
        "heartbeatList": {
            "7": [{"status": 1, "ping": 12, "time": "2026-07-13 10:00:00", "msg": "OK"}],
            "9": [{"status": 0, "ping": None, "time": "2026-07-13 10:00:00"}],
        },
        "uptimeList": {"7_24": 0.999, "9_24": 0.5},
    })
    by_id = {monitor.monitor_id: monitor for monitor in monitors}
    assert by_id["7"].status == "up"
    assert by_id["7"].last_ping_ms == 12
    assert by_id["7"].uptime_24h == 0.999
    assert by_id["9"].status == "down"
    assert "msg" not in by_id["7"].model_dump()


# --- Asterisk ---------------------------------------------------------------


def test_asterisk_core_normalized():
    core = asterisk.normalize_core(
        {"Response": "Success", "CoreStartupDate": "2026-07-01",
         "CoreStartupTime": "08:00:00", "CoreCurrentCalls": "2"},
        {"Response": "Success", "AsteriskVersion": "20.5.0",
         "AMIversion": "9.0.0", "CoreMaxCalls": "10"},
    )
    assert core.version == "20.5.0"
    assert core.startup_time == "2026-07-01 08:00:00"
    assert core.current_calls == "2"
    assert core.max_calls == "10"
    assert "Response" not in core.model_dump()


def test_asterisk_channels_filtered():
    channels = asterisk.normalize_channels([
        {"Event": "CoreShowChannel", "Channel": "PJSIP/100-0001",
         "ChannelStateDesc": "Up", "CallerIDNum": "100", "Application": "Dial",
         "Duration": "00:01:23", "AccountCode": "internal"},
        {"Event": "CoreShowChannelsComplete", "ListItems": "1"},
    ])
    assert len(channels) == 1
    assert channels[0].channel == "PJSIP/100-0001"
    assert channels[0].state == "Up"
    assert "AccountCode" not in channels[0].model_dump()


def test_asterisk_peers_normalized():
    endpoints = asterisk.normalize_pjsip_endpoints([
        {"Event": "EndpointList", "ObjectName": "100", "DeviceState": "Not in use",
         "Contacts": "100/sip:100@10.0.0.9", "Auths": "auth100"},
        {"Event": "EndpointListComplete"},
    ])
    assert len(endpoints) == 1
    assert endpoints[0].endpoint == "100"
    assert "Auths" not in endpoints[0].model_dump()

    peers = asterisk.normalize_sip_peers([
        {"Event": "PeerEntry", "ObjectName": "100", "Status": "OK (12 ms)",
         "IPaddress": "10.0.0.9", "Dynamic": "yes"},
        {"Event": "PeerlistComplete"},
    ])
    assert len(peers) == 1
    assert peers[0].address == "10.0.0.9"
    assert peers[0].dynamic == "yes"


# --- FritzBox ---------------------------------------------------------------


def test_fritzbox_device_info_normalized():
    device = fritzbox.normalize_device_info({
        "manufacturerName": "AVM", "modelName": "FRITZ!Box 7590",
        "serialNumber": "abc", "softwareVersion": "8.00",
        "hardwareVersion": "123", "upTime": "42",
        "provisioningCode": "000.044.000.000",  # vendor extra, must be dropped
    })
    assert device.manufacturer == "AVM"
    assert device.model == "FRITZ!Box 7590"
    assert device.uptime_seconds == 42
    assert "provisioningCode" not in device.model_dump()


def test_fritzbox_wan_normalized():
    wan = fritzbox.normalize_wan(
        {"wanAccessType": "DSL", "physicalLinkStatus": "Up",
         "layer1UpstreamMaxBitRate": "50000000", "layer1DownstreamMaxBitRate": "200000000"},
        {"totalBytesSent": "100"},
        {"totalBytesReceived": "250"},
    )
    assert wan.physical_link_status == "Up"
    assert wan.upstream_max_mbps == 50
    assert wan.downstream_max_mbps == 200
    assert wan.bytes_sent == 100
    assert wan.bytes_received == 250


def test_fritzbox_wifi_radio_normalized():
    radio = fritzbox.normalize_wifi_radio(1, {
        "enable": "1", "status": "Up", "ssid": "lab", "channel": "6",
        "standard": "n", "beaconType": "11i", "wlanMACAddress": "AA:BB",
    })
    assert radio.index == 1
    assert radio.enabled is True
    assert radio.channel == 6
    assert "wlanMACAddress" not in radio.model_dump()


# --- Cloudflare Tunnel ------------------------------------------------------


def test_cloudflaretunnel_api_status_normalized_without_raw_metadata():
    tunnel_id = "11111111-2222-4333-8444-555555555555"
    tunnel = cloudflaretunnel.normalize_tunnel(
        {
            "success": True,
            "result": {
                "id": tunnel_id,
                "name": "home",
                "status": "healthy",
                "config_src": "cloudflare",
                "metadata": {"private": "discarded"},
                "connections": [{"origin_ip": "discarded"}],
            },
        },
        tunnel_id,
    )
    assert tunnel.status == "healthy"
    assert tunnel.config_source == "cloudflare"
    assert "metadata" not in tunnel.model_dump()
    assert "connections" not in tunnel.model_dump()


def test_cloudflaretunnel_connectors_are_aggregated_and_redacted():
    tunnel_id = "11111111-2222-4333-8444-555555555555"
    connectors = cloudflaretunnel.normalize_connectors(
        {
            "success": True,
            "result": [
                {
                    "id": "private-connector-id",
                    "version": "2026.7.0",
                    "arch": "amd64",
                    "conns": [
                        {"origin_ip": "198.51.100.2", "is_pending_reconnect": False}
                    ],
                }
            ],
        },
        tunnel_id,
    )
    assert connectors[0].connections_active == 1
    assert "id" not in connectors[0].model_dump()
    assert "origin_ip" not in connectors[0].model_dump()


# --- Proxmox disk temperatures ----------------------------------------------


def test_proxmox_disk_temperature_ata_attribute():
    from app.providers.proxmox import normalizers as proxmox

    disk = {"devpath": "/dev/sda", "model": "PNY_CS900_480GB_SSD", "type": "ssd", "serial": "S3CR3T"}
    smart = {
        "type": "ata",
        "attributes": [
            # The HTTP API serializes ids as strings; pvesh shows them as ints.
            {"id": "190", "name": "Airflow_Temperature_Cel", "raw": "38"},
            {"id": "194", "name": "Temperature_Celsius", "raw": "33 (Min/Max 33/33)"},
        ],
    }
    result = proxmox.normalize_disk_temperature("pve", disk, smart)
    assert result.temperature_c == 33
    assert result.node == "pve"
    assert result.devpath == "/dev/sda"
    assert "S3CR3T" not in str(result.model_dump())


def test_proxmox_disk_temperature_nvme_text():
    from app.providers.proxmox import normalizers as proxmox

    smart = {"type": "text", "text": "SMART/Health Information\nTemperature: 41 Celsius\nAvailable Spare: 100%"}
    result = proxmox.normalize_disk_temperature("pve2", {"devpath": "/dev/nvme0n1", "model": "X", "type": "nvme"}, smart)
    assert result.temperature_c == 41


def test_proxmox_disk_temperature_missing_is_none():
    from app.providers.proxmox import normalizers as proxmox

    result = proxmox.normalize_disk_temperature("pve", {"devpath": "/dev/sdb"}, {"type": "unknown"})
    assert result.temperature_c is None


# --- FritzBox ecoStat temperature -------------------------------------------


def test_fritzbox_ecostat_temperature_latest_sample():
    result = fritzbox.normalize_ecostat_temperature({
        "data": {
            "cputemp": {"series": [["77", "77", "79", "82"]]},
            "cpuutil": {"series": [["12", "9"]]},
        }
    })
    assert result.supported is True
    assert result.maximum_temperature_c == 82
    assert result.sensors[0].sensor_id == "cpu"
    assert result.sensors[0].kind == "cpu"


def test_fritzbox_ecostat_temperature_no_sensor():
    # Repeaters expose the ecoStat page with an always-empty series.
    result = fritzbox.normalize_ecostat_temperature({"data": {"cputemp": {"series": [], "labels": [False, 13]}}})
    assert result.supported is False
    assert result.sensors == []
    assert result.maximum_temperature_c is None


# --- Glances host temperatures ----------------------------------------------


def test_glances_sensors_parsed():
    from app.providers.glances import normalizers as glances

    raw = [
        {"label": "Core 0", "unit": "C", "value": 42, "warning": 105, "type": "temperature_core", "key": "label"},
        {"label": "Package id 0", "unit": "C", "value": 43, "warning": 105, "type": "temperature_core", "key": "label"},
        {"label": "fan1", "unit": "R", "value": 1200, "type": "fan_speed", "key": "label"},
        {"label": "broken", "unit": "C", "value": None, "type": "temperature_core", "key": "label"},
    ]
    result = glances.normalize_host_sensors("pve", "http://10.0.0.5:61208", raw)
    assert result.error is None
    assert [item.sensor_id for item in result.sensors] == ["core_0", "package_id_0"]
    assert {item.kind for item in result.sensors} == {"cpu"}
    assert result.maximum_temperature_c == 43
    assert "warning" not in str(result.model_dump())


def test_glances_raspberry_soc_parsed():
    from app.providers.glances import normalizers as glances

    raw = [{"label": "cpu_thermal 0", "unit": "C", "value": 60.1, "type": "temperature_core", "key": "label"}]
    result = glances.normalize_host_sensors("pizero1", "http://10.0.0.8:61208", raw)
    assert result.sensors[0].kind == "cpu"
    assert result.maximum_temperature_c == 60.1


def test_glances_empty_payload_has_no_sensors():
    from app.providers.glances import normalizers as glances

    result = glances.normalize_host_sensors("qdevice", "http://10.0.0.9:61208", [])
    assert result.sensors == []
    assert result.maximum_temperature_c is None
