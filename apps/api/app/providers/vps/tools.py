"""VPS provider and read-only diagnostics tools."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP
from app.providers.vps.client import VpsGlancesClient
from app.providers.vps.normalizers import normalize_deploy_targets, normalize_glances_all, normalize_wireguard
from app.services.inventory import get_host, get_http_target, provider_config
from app.tools import netcheck


class VpsProvider(Provider):
    id = "vps"
    display_name = "VPS"
    credential_requirements = ("providers.vps.host_id", "providers.vps.glances.base_url")

    def client(self) -> VpsGlancesClient:
        return VpsGlancesClient()

    def ready(self) -> bool:
        return bool(_host_id() and get_host(_host_id())) and self.client().is_configured()

    async def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        if not _host_id() or get_host(_host_id()) is None:
            return ProviderHealth(provider_id=self.id, status="unavailable", detail="vps host_id is not configured", checked_at=now)
        client = self.client()
        if not client.is_configured():
            return ProviderHealth(provider_id=self.id, status="misconfigured", detail="vps glances base_url is not configured", checked_at=now)
        try:
            await client.all(timeout=4.0)
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def reachability_status() -> dict:
    host_id = _host_id()
    if not host_id:
        return {"ok": False, "error": "vps_host_id_missing"}
    return await netcheck.host_check(host_id, None, float(_config().get("reachability_timeout", 2.0)))


async def glances_status() -> dict:
    raw = await VpsGlancesClient().all()
    return {"glances": normalize_glances_all(raw).model_dump()}


async def wireguard_status() -> dict:
    config = _config()
    interface_names = [str(item) for item in config.get("wireguard_interfaces", []) if str(item)]
    raw = await VpsGlancesClient().all()
    route_targets = await _route_target_checks(config)
    return {"wireguard": normalize_wireguard(raw, interface_names, route_targets).model_dump()}


async def deploy_status() -> dict:
    config = _config()
    target_ids = [str(item) for item in config.get("deploy_http_target_ids", []) if str(item)]
    timeout = float(config.get("deploy_timeout", 8.0))
    rows = await asyncio.gather(*(_check_http_target(target_id, timeout) for target_id in target_ids))
    return {"deploy": normalize_deploy_targets(list(rows)).model_dump()}


async def summary() -> dict:
    health, reachability, glances, wireguard, deploy = await asyncio.gather(
        VpsProvider().health(),
        _safe(reachability_status()),
        _safe(glances_status()),
        _safe(wireguard_status()),
        _safe(deploy_status()),
    )
    findings: list[dict[str, str]] = []
    metrics: dict[str, Any] = {
        "health_status": health.status,
        "host_reachable": reachability.get("ok") if isinstance(reachability, dict) else False,
    }

    if health.status != "healthy":
        findings.append({"severity": "critical" if health.status in {"unreachable", "misconfigured", "unavailable"} else "warning", "message": health.detail or health.status})

    resources = ((glances.get("glances") or {}).get("resources") or {}) if isinstance(glances, dict) else {}
    metrics.update({
        "cpu_percent": resources.get("cpu_percent"),
        "memory_percent": resources.get("memory_percent"),
        "disk_percent_max": resources.get("disk_percent_max"),
    })
    if (resources.get("disk_percent_max") or 0) >= 90:
        findings.append({"severity": "critical", "message": "VPS disk usage above 90%"})
    elif (resources.get("disk_percent_max") or 0) >= 85:
        findings.append({"severity": "warning", "message": "VPS disk usage above 85%"})
    if (resources.get("memory_percent") or 0) >= 90:
        findings.append({"severity": "warning", "message": "VPS memory usage above 90%"})

    wg = (wireguard.get("wireguard") or {}) if isinstance(wireguard, dict) else {}
    deploy_body = (deploy.get("deploy") or {}) if isinstance(deploy, dict) else {}
    metrics.update({
        "wireguard_interfaces": len(wg.get("interfaces", [])),
        "wireguard_routes_total": len(wg.get("route_targets", [])),
        "deploy_targets_total": deploy_body.get("total", 0),
        "deploy_targets_unhealthy": deploy_body.get("unhealthy", 0),
    })
    if wg and not wg.get("ok"):
        findings.append({"severity": "warning", "message": "VPS WireGuard observation is incomplete or route targets are unreachable"})
    if deploy_body.get("unhealthy"):
        findings.append({"severity": "critical", "message": f"{deploy_body.get('unhealthy')} VPS deploy target(s) unhealthy"})

    status = health.status
    if status == "healthy" and findings:
        status = "degraded"
    return {
        "summary": {
            "provider_id": "vps",
            "status": status,
            "severity": _severity(status, findings),
            "generated_at": datetime.now(UTC).isoformat(),
            "metrics": metrics,
            "findings": findings[:12],
            "next_actions": [item["message"] for item in findings[:5]],
        }
    }


def _config() -> dict[str, Any]:
    return provider_config("vps")


def _host_id() -> str:
    return str(_config().get("host_id") or "")


async def _safe(coro) -> dict[str, Any]:
    try:
        return await coro
    except ProviderError as exc:
        return {"unavailable": exc.code, "detail": exc.message}
    except Exception as exc:
        return {"unavailable": "error", "detail": exc.__class__.__name__}


async def _route_target_checks(config: dict[str, Any]) -> list[dict[str, Any]]:
    target_ids = [str(item) for item in config.get("wireguard_route_host_ids", []) if str(item)]
    timeout = float(config.get("wireguard_route_timeout", 2.0))
    rows = []
    for host_id in target_ids:
        host = get_host(host_id)
        if host is None:
            rows.append({"host_id": host_id, "ok": False, "error": "unknown_host_id"})
            continue
        check = await netcheck.host_check(host_id, None, timeout)
        rows.append({"host_id": host_id, "host_name": host.name, "ok": bool(check.get("ok")), "checks": check.get("checks", [])})
    return rows


async def _check_http_target(target_id: str, timeout: float) -> dict[str, Any]:
    target = get_http_target(target_id)
    if target is None:
        return {"id": target_id, "url": "", "ok": False, "error": "unknown_http_target_id"}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(target.url, headers={"User-Agent": "GuixOS-Homelab-Console/0.1"})
    except httpx.TimeoutException:
        return {"id": target.id, "name": target.name, "url": target.url, "ok": False, "error": "timeout"}
    except httpx.HTTPError as exc:
        return {"id": target.id, "name": target.name, "url": target.url, "ok": False, "error": exc.__class__.__name__}
    duration_ms = int((time.perf_counter() - started) * 1000)
    expected = target.expected_statuses or [200, 301, 302, 307, 308]
    return {
        "id": target.id,
        "name": target.name,
        "url": target.url,
        "ok": response.status_code in expected,
        "status_code": response.status_code,
        "duration_ms": duration_ms,
    }


def _severity(status: str, findings: list[dict[str, str]]) -> str:
    if status in {"unreachable", "misconfigured", "unavailable"}:
        return "critical"
    if any(item["severity"] == "critical" for item in findings):
        return "critical"
    if status in {"degraded", "unknown"} or any(item["severity"] == "warning" for item in findings):
        return "warning"
    return "ok"
