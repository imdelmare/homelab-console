import httpx

from app.domain.actors import Actor
from app.tools.execution import execute_tool

OPERATOR = Actor(kind="user", id="operator", label="operator")


def _mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.httpclient.httpx.AsyncClient", client_factory)
    monkeypatch.setattr("app.providers.vps.tools.httpx.AsyncClient", client_factory)


def _glances_payload():
    return {
        "system": {"hostname": "vps01", "os_name": "Linux", "linux_distro": "Debian"},
        "cpu": {"total": 12.5},
        "mem": {"percent": 44.2},
        "load": {"min1": 0.12, "min5": 0.2, "min15": 0.3},
        "fs": [{"mnt_point": "/", "percent": 61.0}],
        "network": [{"interface_name": "wg0", "rx": 1234, "tx": 5678}],
        "uptime": {"seconds": 12345},
    }


async def test_vps_glances_status_normalized(monkeypatch):
    monkeypatch.setattr(
        "app.providers.vps.client.provider_config",
        lambda _pid: {"glances": {"base_url": "http://vps.test:61208", "timeout_seconds": 3}},
    )

    def handler(request):
        assert str(request.url) == "http://vps.test:61208/api/4/all"
        return httpx.Response(200, json=_glances_payload())

    _mock_transport(monkeypatch, handler)
    result = await execute_tool("vps.glances.status", {}, OPERATOR)
    assert result.ok is True
    assert result.result is not None
    glances = result.result["glances"]
    assert glances["ok"] is True
    assert glances["system"]["hostname"] == "vps01"
    assert glances["resources"]["cpu_percent"] == 12.5
    assert glances["resources"]["disk_percent_max"] == 61.0


async def test_vps_wireguard_status_uses_declared_interfaces_and_targets(monkeypatch):
    from app.services.inventory import HostEntry

    monkeypatch.setattr(
        "app.providers.vps.tools.provider_config",
        lambda _pid: {
            "wireguard_interfaces": ["wg0"],
            "wireguard_route_host_ids": ["opnsense"],
        },
    )

    async def fake_all(self):
        return _glances_payload()

    async def fake_host_check(host_id, ports, timeout):
        return {"ok": True, "host_id": host_id, "checks": [{"port": 443, "open": True}]}

    monkeypatch.setattr("app.providers.vps.tools.VpsGlancesClient.all", fake_all)
    monkeypatch.setattr("app.providers.vps.tools.get_host", lambda host_id: HostEntry(id=host_id, name="OPNsense", address="10.0.0.1"))
    monkeypatch.setattr("app.providers.vps.tools.netcheck.host_check", fake_host_check)

    result = await execute_tool("vps.wireguard.status", {}, OPERATOR)
    assert result.ok is True
    assert result.result is not None
    wireguard = result.result["wireguard"]
    assert wireguard["ok"] is True
    assert wireguard["interfaces"][0]["name"] == "wg0"
    assert wireguard["interfaces"][0]["present"] is True
    assert wireguard["route_targets"][0]["ok"] is True
    assert "peer handshake age" in wireguard["limitations"][0]


async def test_vps_deploy_status_uses_declared_http_targets(monkeypatch):
    from app.services.inventory import HttpTargetEntry

    monkeypatch.setattr(
        "app.providers.vps.tools.provider_config",
        lambda _pid: {"deploy_http_target_ids": ["public-console"]},
    )
    monkeypatch.setattr(
        "app.providers.vps.tools.get_http_target",
        lambda target_id: HttpTargetEntry(
            id=target_id,
            name="Example Organization",
            url="https://public-console.test",
            expected_statuses=[200],
        ),
    )

    _mock_transport(monkeypatch, lambda request: httpx.Response(200, text="ok"))
    result = await execute_tool("vps.deploy.status", {}, OPERATOR)
    assert result.ok is True
    assert result.result is not None
    deploy = result.result["deploy"]
    assert deploy["ok"] is True
    assert deploy["total"] == 1
    assert deploy["targets"][0]["id"] == "public-console"


async def test_vps_tools_are_registered():
    from app.tools.registry import get_tool

    for tool_id in ("vps.glances.status", "vps.wireguard.status", "vps.deploy.status", "vps.summary"):
        tool = get_tool(tool_id)
        assert tool is not None
        assert tool.provider_id == "vps"
