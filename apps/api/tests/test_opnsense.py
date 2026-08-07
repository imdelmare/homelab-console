"""OPNsense normalization tests: vendor payloads in, stable shapes out."""

import httpx
import pytest

from app.providers.errors import ProviderError
from app.providers.opnsense.client import OpnsenseClient
from app.providers.opnsense.tools import OpnsenseProvider


def _configure(monkeypatch, **extra):
    secrets = {
        "base_url": "https://opnsense.test",
        "api_key": "key",
        "api_secret": "sec",
        "verify_tls": True,
        **extra,
    }
    monkeypatch.setattr(
        "app.providers.opnsense.client.get_provider_secrets", lambda _pid: secrets, raising=True
    )


def _mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("verify", None)
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.httpclient.httpx.AsyncClient", client_factory)


async def test_firmware_status_normalized(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        assert request.headers.get("authorization", "").startswith("Basic ")
        return httpx.Response(200, json={
            "status": "update", "status_msg": "There are 3 updates available.",
            "needs_reboot": "1", "os_version": "FreeBSD 14.1-RELEASE-p6",
            "product": {"product_version": "24.7.5", "product_latest": "24.7.7",
                        "product_check": {"upgrade_needs_reboot": "0"}},
            "upgrade_packages": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
            "new_packages": [], "reinstall_packages": [],
            "last_check": "Sun Jul 13 10:00:01 CEST 2026",
            "download_size": "12.3MiB",  # vendor extra, must be dropped
        })

    _mock_transport(monkeypatch, handler)
    from app.providers.opnsense.tools import firmware_status

    firmware = (await firmware_status())["firmware"]
    assert firmware["product_version"] == "24.7.5"
    assert firmware["product_latest"] == "24.7.7"
    assert firmware["needs_reboot"] is True
    assert firmware["upgrade_packages"] == 3
    assert firmware["status"] == "update"
    assert "download_size" not in firmware


async def test_system_status_subsystems(monkeypatch):
    _configure(monkeypatch)
    _mock_transport(monkeypatch, lambda request: httpx.Response(200, json={
        "CrashReporter": {"statusCode": 2, "message": "No problems were detected."},
        "Firewall": {"status": "OK"},
        "System": {"status": "OK", "age": "n/a"},
    }))
    from app.providers.opnsense.tools import system_status

    status = (await system_status())["status"]
    assert status["total"] == 3
    by_name = {entry["subsystem"]: entry for entry in status["subsystems"]}
    assert by_name["CrashReporter"]["status"] == "2"
    assert by_name["Firewall"]["status"] == "OK"


async def test_system_information_normalized(monkeypatch):
    _configure(monkeypatch)
    _mock_transport(monkeypatch, lambda request: httpx.Response(200, json={
        "name": "opnsense.lab.local",
        "versions": ["OPNsense 24.7.5", "FreeBSD 14.1-RELEASE-p6", "OpenSSL 3.0.15"],
        "date": "Sun Jul 13 10:05:00 CEST 2026",
    }))
    from app.providers.opnsense.tools import system_information

    system = (await system_information())["system"]
    assert system["hostname"] == "opnsense.lab.local"
    assert len(system["versions"]) == 3


async def test_system_resources_memory_percent(monkeypatch):
    _configure(monkeypatch)
    _mock_transport(monkeypatch, lambda request: httpx.Response(200, json={
        "memory": {"total": "8589934592", "total_frmt": "8 GiB",
                   "used": "2147483648", "used_frmt": "2 GiB"},
    }))
    from app.providers.opnsense.tools import system_resources

    resources = (await system_resources())["resources"]
    assert resources["memory_total_bytes"] == 8589934592
    assert resources["memory_used_bytes"] == 2147483648
    assert resources["memory_used_percent"] == 25.0


async def test_system_temperature(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        assert request.url.path == "/api/diagnostics/system/system_temperature"
        return httpx.Response(200, json=[
            {
                "device": "dev.cpu.0.temperature",
                "device_seq": "0",
                "temperature": "48.1 C",
                "type": "cpu",
                "type_translated": "CPU",
            },
            {
                "device": "hw.acpi.thermal.tz0.temperature",
                "device_seq": "",
                "temperature": "42.0",
                "type": "zone",
            },
        ])

    _mock_transport(monkeypatch, handler)
    from app.providers.opnsense.tools import system_temperature

    result = await system_temperature()
    assert result["temperature"]["maximum_temperature_c"] == 48.1
    assert result["temperature"]["sensors"][0] == {
        "sensor_id": "dev.cpu.0.temperature.0",
        "kind": "cpu",
        "temperature_c": 48.1,
    }


async def test_interface_names(monkeypatch):
    _configure(monkeypatch)
    _mock_transport(monkeypatch, lambda request: httpx.Response(200, json={
        "igb0": "WAN", "igb1": "LAN", "wg0": "WireGuard",
    }))
    from app.providers.opnsense.tools import interface_names

    result = await interface_names()
    assert result["total"] == 3
    assert {"device": "igb1", "name": "LAN"} in result["interfaces"]


async def test_interface_statistics_normalized(monkeypatch):
    _configure(monkeypatch)
    _mock_transport(monkeypatch, lambda request: httpx.Response(200, json={
        "interfaces": {
            "igb0": {"name": "igb0", "flags": "8863", "mtu": 1500,
                     "bytes received": "123456789", "bytes transmitted": "987654321",
                     "packets received": "1000", "packets transmitted": "2000",
                     "input errors": "0", "output errors": "1", "collisions": "0"},
        }
    }))
    from app.providers.opnsense.tools import interface_statistics

    result = await interface_statistics()
    assert result["total"] == 1
    stats = result["statistics"][0]
    assert stats["device"] == "igb0"
    assert stats["rx_bytes"] == 123456789
    assert stats["tx_bytes"] == 987654321
    assert stats["tx_errors"] == 1
    assert "flags" not in stats and "mtu" not in stats


async def test_arp_table_normalized(monkeypatch):
    # OPNsense's /api/diagnostics/interface/get_arp responds with a BARE
    # JSON array, not a dict wrapping the rows — verified directly against
    # a real firewall. This is the actual response shape, not a variant.
    _configure(monkeypatch)

    def handler(request):
        assert request.url.path == "/api/diagnostics/interface/get_arp"
        return httpx.Response(200, json=[
            {
                "ip": "10.0.0.101",
                "mac": "aa:bb:cc:dd:ee:ff",
                "manufacturer": "Raspberry Pi Trading",
                "interface": "igb1",
                "interface_name": "LAN",
                "hostname": "homeassistant",
                "expires": "1199",
                "raw_vendor_field": "drop me",
            },
            {"ip": "", "mac": ""},
        ])

    _mock_transport(monkeypatch, handler)
    from app.providers.opnsense.tools import arp_table

    result = await arp_table()
    assert result["total"] == 1
    device = result["devices"][0]
    assert device["ip_address"] == "10.0.0.101"
    assert device["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert device["hostname"] == "homeassistant"
    assert device["interface_name"] == "LAN"
    assert "raw_vendor_field" not in device


async def test_arp_table_also_accepts_dict_wrapped_rows(monkeypatch):
    # Defensive: some OPNsense versions/plugins might wrap rows in a dict
    # like every other endpoint this module normalizes. The fix must not
    # regress that shape even though it's not what's seen in practice.
    _configure(monkeypatch)

    def handler(request):
        return httpx.Response(200, json={
            "rows": [{"ip": "10.0.0.102", "mac": "11:22:33:44:55:66"}],
        })

    _mock_transport(monkeypatch, handler)
    from app.providers.opnsense.tools import arp_table

    result = await arp_table()
    assert result["total"] == 1
    assert result["devices"][0]["ip_address"] == "10.0.0.102"


async def test_kea_leases_normalized(monkeypatch):
    # IPv4 leases live under .../leases4/... — verified directly against a
    # real firewall; the unversioned .../leases/search path 404s.
    _configure(monkeypatch)

    def handler(request):
        assert request.url.path == "/api/kea/leases4/search"
        return httpx.Response(200, json={
            "rows": [
                {
                    "address": "10.0.0.42",
                    "hwaddr": "11:22:33:44:55:66",
                    "hostname": "workstation",
                    "subnet_id": "10",
                    "state": "active",
                    "starts": "2026-07-13 10:00:00",
                    "ends": "2026-07-13 22:00:00",
                    "valid_lifetime": "43200",
                    "raw_vendor_field": "drop me",
                },
                {"address": "", "hwaddr": ""},
            ],
            "rowCount": 1,
        })

    _mock_transport(monkeypatch, handler)
    from app.providers.opnsense.tools import kea_leases

    result = await kea_leases()
    assert result["total"] == 1
    assert result["source_endpoint"] == "/api/kea/leases4/search"
    lease = result["leases"][0]
    assert lease["ip_address"] == "10.0.0.42"
    assert lease["mac_address"] == "11:22:33:44:55:66"
    assert lease["hostname"] == "workstation"
    assert lease["subnet_id"] == "10"
    assert lease["state"] == "active"
    assert lease["valid_lifetime_seconds"] == 43200
    assert "raw_vendor_field" not in lease


async def test_kea_leases_falls_back_to_dhcpv4_lease_controller(monkeypatch):
    _configure(monkeypatch)
    seen_paths = []

    def handler(request):
        seen_paths.append(request.url.path)
        if request.url.path == "/api/kea/leases4/search":
            return httpx.Response(404, json={})
        assert request.url.path == "/api/dhcpv4/leases/searchLease"
        return httpx.Response(200, json={
            "rows": [
                {
                    "address": "10.0.0.43",
                    "hwaddr": "22:33:44:55:66:77",
                    "hostname": "tablet",
                    "if_descr": "LAN",
                    "state": 0,
                },
            ],
        })

    _mock_transport(monkeypatch, handler)
    from app.providers.opnsense.tools import kea_leases

    result = await kea_leases()
    assert seen_paths == ["/api/kea/leases4/search", "/api/dhcpv4/leases/searchLease"]
    assert result["source_endpoint"] == "/api/dhcpv4/leases/searchLease"
    assert result["total"] == 1
    assert result["leases"][0]["interface"] == "LAN"
    assert result["leases"][0]["state"] == "active"


async def test_gateway_status_normalized(monkeypatch):
    _configure(monkeypatch)
    _mock_transport(monkeypatch, lambda request: httpx.Response(200, json={
        "items": [
            {"name": "WAN_GW", "address": "192.0.2.1", "status": "none",
             "status_translated": "Online", "loss": "0.0 %", "delay": "12.3 ms",
             "stddev": "1.2 ms", "monitor": "192.0.2.1"},
            {"name": "LTE_GW", "address": "198.51.100.1", "status": "down",
             "status_translated": "Offline", "loss": "100.0 %", "delay": "~",
             "stddev": "~"},
        ],
        "status": "ok",
    }))
    from app.providers.opnsense.tools import gateway_status

    result = await gateway_status()
    assert result["total"] == 2
    wan, lte = result["gateways"]
    assert wan["online"] is True
    assert wan["rtt_ms"] == 12.3
    assert wan["loss_percent"] == 0.0
    assert lte["online"] is False
    assert lte["rtt_ms"] is None
    assert result["offline"] == ["LTE_GW"]
    assert "monitor" not in wan


async def test_auth_error_no_secret_leak(monkeypatch):
    _configure(monkeypatch)
    _mock_transport(monkeypatch, lambda request: httpx.Response(401, json={}))
    with pytest.raises(ProviderError) as exc_info:
        await OpnsenseClient().get("/api/core/system/status")
    assert exc_info.value.code == "auth_failed"
    assert "sec" != str(exc_info.value)  # sanity
    assert "key" not in str(exc_info.value).replace("API key", "")


async def test_unconfigured_health_unavailable():
    health = await OpnsenseProvider().health()
    assert health.status == "unavailable"
