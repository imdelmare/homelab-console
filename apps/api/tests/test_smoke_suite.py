from tests.conftest import do_login


async def test_suite_1_public_health_and_auth_config(client):
    health = await client.get("/health")
    assert health.status_code == 200, health.text
    assert health.json() == {"status": "ok"}

    config = await client.get("/api/auth/config")
    assert config.status_code == 200, config.text
    assert config.json()["app_name"]

    session = await client.get("/api/auth/session")
    assert session.status_code == 200, session.text
    assert session.json() == {"authenticated": False}


async def test_suite_1_authenticated_core_surfaces(client, user, capture_adapter):
    body, csrf = await do_login(client, capture_adapter)
    assert body["authenticated"] is True
    assert csrf

    session = await client.get("/api/auth/session")
    assert session.status_code == 200, session.text
    assert session.json()["authenticated"] is True

    list_endpoints = [
        "/api/tools",
        "/api/tasks",
        "/api/providers",
        "/api/watchers/incidents",
        "/api/watchers/runs",
        "/api/mcp/clients",
        "/api/audit",
        "/api/inventory/hosts",
    ]
    for endpoint in list_endpoints:
        response = await client.get(endpoint)
        assert response.status_code == 200, f"{endpoint}: {response.text}"
        assert isinstance(response.json(), list), endpoint

    watcher_status = await client.get("/api/watchers/status")
    assert watcher_status.status_code == 200, watcher_status.text
    assert watcher_status.json()["watcher_ids"] == [
        "backup.freshness",
        "cloudflare.tunnel",
        "lab.alerts",
        "network.gateway",
        "network.presence",
        "network.wireguard",
        "network.zerotier",
        "power.ups",
        "security.certificates",
        "storage.disks",
        "thermal.sensors",
        "uptimekuma.monitors",
    ]
    assert watcher_status.json()["scheduled_watcher_ids"] == [
        "cloudflare.tunnel",
        "lab.alerts",
        "network.gateway",
        "network.wireguard",
        "network.zerotier",
        "power.ups",
        "uptimekuma.monitors",
    ]

    ops_health = await client.get("/api/ops/health")
    assert ops_health.status_code == 200, ops_health.text
    assert ops_health.json()["database"]["dialect"]
    assert "notification_counts" in ops_health.json()["workers"]
