import httpx

from app.domain.actors import Actor
from app.tools.execution import execute_tool

OPERATOR = Actor(kind="user", id="operator", label="operator")


def _configure(monkeypatch):
    monkeypatch.setattr(
        "app.providers.pbs.client.provider_config",
        lambda _pid: {"base_url": "https://pbs.test:8007", "verify_tls": True, "timeout_seconds": 3},
    )
    monkeypatch.setattr(
        "app.providers.pbs.client.get_provider_secrets",
        lambda _pid: {"api_token_id": "root@pam!console", "api_token_secret": "secret"},
    )


def _mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr("app.providers.httpclient.httpx.AsyncClient", client_factory)


async def test_pbs_version_uses_api_token_header(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        assert request.headers["authorization"] == "PBSAPIToken=root@pam!console:secret"
        assert str(request.url) == "https://pbs.test:8007/api2/json/version"
        return httpx.Response(200, json={"data": {"version": "3.4.1", "release": "1"}})

    _mock_transport(monkeypatch, handler)
    result = await execute_tool("pbs.version", {}, OPERATOR)
    assert result.ok is True
    assert result.result is not None
    assert result.result["version"]["version"] == "3.4.1"


async def test_pbs_datastores_status(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        if str(request.url).endswith("/api2/json/admin/datastore"):
            return httpx.Response(200, json={"data": [{"store": "main"}]})
        if str(request.url).endswith("/api2/json/admin/datastore/main/status"):
            return httpx.Response(200, json={"data": {"total": 1000, "used": 900, "avail": 100}})
        raise AssertionError(str(request.url))

    _mock_transport(monkeypatch, handler)
    result = await execute_tool("pbs.datastores.status", {}, OPERATOR)
    assert result.ok is True
    assert result.result is not None
    assert result.result["datastores"][0]["used_percent"] == 90.0
    assert result.result["high_usage"] == ["main"]


async def test_pbs_backup_jobs_health_groups_snapshots(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        if str(request.url).endswith("/api2/json/admin/datastore"):
            return httpx.Response(200, json={"data": [{"store": "main"}]})
        if str(request.url).endswith("/api2/json/admin/datastore/main/snapshots"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"backup-type": "vm", "backup-id": "100", "backup-time": 100},
                        {"backup-type": "vm", "backup-id": "100", "backup-time": 200},
                        {"backup-type": "ct", "backup-id": "101", "backup-time": 150},
                    ]
                },
            )
        raise AssertionError(str(request.url))

    _mock_transport(monkeypatch, handler)
    result = await execute_tool("pbs.backup.jobs.health", {}, OPERATOR)
    assert result.ok is True
    assert result.result is not None
    assert result.result["groups_total"] == 2
    vm = next(item for item in result.result["backup_groups"] if item["backup_id"] == "100")
    assert vm["latest_backup_at"] == 200
    assert vm["snapshots_count"] == 2


async def test_pbs_summary_flags_failed_tasks(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        url = str(request.url)
        if url.endswith("/api2/json/version"):
            return httpx.Response(200, json={"data": {"version": "3.4.1"}})
        if url.endswith("/api2/json/admin/datastore"):
            return httpx.Response(200, json={"data": [{"store": "main"}]})
        if url.endswith("/api2/json/admin/datastore/main/status"):
            return httpx.Response(200, json={"data": {"total": 1000, "used": 100, "avail": 900}})
        if url.endswith("/api2/json/nodes/localhost/tasks?limit=50"):
            return httpx.Response(200, json={"data": [{"upid": "UPID:1", "status": "TASK ERROR"}]})
        if url.endswith("/api2/json/config/verify"):
            return httpx.Response(200, json={"data": [{"id": "verify-main", "store": "main"}]})
        if url.endswith("/api2/json/admin/datastore/main/snapshots"):
            return httpx.Response(200, json={"data": [{"backup-type": "vm", "backup-id": "100", "backup-time": 200}]})
        raise AssertionError(url)

    _mock_transport(monkeypatch, handler)
    result = await execute_tool("pbs.summary", {}, OPERATOR)
    assert result.ok is True
    assert result.result is not None
    summary = result.result["summary"]
    assert summary["provider_id"] == "pbs"
    assert summary["status"] == "degraded"
    assert summary["metrics"]["recent_tasks_failed"] == 1
    assert summary["metrics"]["backup_groups_stale"] == 1
    assert summary["metrics"]["backup_oldest_age_days"] is not None
    assert "backup_groups_stale" in {item.get("code") for item in summary["findings"]}


async def test_pbs_summary_flags_missing_verify_jobs(monkeypatch):
    _configure(monkeypatch)

    def handler(request):
        url = str(request.url)
        if url.endswith("/api2/json/version"):
            return httpx.Response(200, json={"data": {"version": "3.4.1"}})
        if url.endswith("/api2/json/admin/datastore"):
            return httpx.Response(200, json={"data": [{"store": "main"}]})
        if url.endswith("/api2/json/admin/datastore/main/status"):
            return httpx.Response(200, json={"data": {"total": 1000, "used": 100, "avail": 900}})
        if url.endswith("/api2/json/nodes/localhost/tasks?limit=50"):
            return httpx.Response(200, json={"data": []})
        if url.endswith("/api2/json/config/verify"):
            return httpx.Response(200, json={"data": []})
        if url.endswith("/api2/json/admin/datastore/main/snapshots"):
            return httpx.Response(200, json={"data": [{"backup-type": "vm", "backup-id": "100", "backup-time": 9999999999}]})
        raise AssertionError(url)

    _mock_transport(monkeypatch, handler)
    result = await execute_tool("pbs.summary", {}, OPERATOR)
    assert result.ok is True
    assert result.result is not None
    findings = result.result["summary"]["findings"]
    assert "verify_jobs_missing" in {item.get("code") for item in findings}
