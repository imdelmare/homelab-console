"""Proxmox Backup Server provider and read-only tools."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP
from app.providers.pbs.client import PbsClient
from app.providers.pbs import normalizers
from app.services.inventory import provider_config


class PbsProvider(Provider):
    id = "pbs"
    display_name = "Proxmox Backup Server"
    credential_requirements = ("pbs.base_url", "pbs.api_token_id", "pbs.api_token_secret")

    def client(self) -> PbsClient:
        return PbsClient()

    def ready(self) -> bool:
        client = self.client()
        return client.is_configured() and client.has_credentials()

    async def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        client = self.client()
        if not client.is_configured():
            return ProviderHealth(provider_id=self.id, status="unavailable", detail="not configured", checked_at=now)
        if not client.has_credentials():
            return ProviderHealth(provider_id=self.id, status="misconfigured", detail="API token not configured", checked_at=now)
        try:
            await client.get("/api2/json/version", timeout=4.0)
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def version() -> dict:
    raw = await PbsClient().get("/api2/json/version")
    return {"version": normalizers.normalize_version(raw).model_dump()}


async def datastores_status() -> dict:
    client = PbsClient()
    stores_raw = await client.get("/api2/json/admin/datastore")
    store_names = [
        str(item.get("store") or item.get("name") or "")
        for item in stores_raw or []
        if isinstance(item, dict) and (item.get("store") or item.get("name"))
    ]
    status_results = await asyncio.gather(
        *(client.get(f"/api2/json/admin/datastore/{name}/status") for name in store_names),
        return_exceptions=True,
    )
    statuses = {}
    for name, result in zip(store_names, status_results):
        if isinstance(result, ProviderError):
            statuses[name] = {"error": result.code}
        elif isinstance(result, Exception):
            statuses[name] = {"error": result.__class__.__name__}
        elif isinstance(result, dict):
            statuses[name] = result
    stores = normalizers.normalize_datastores(stores_raw, statuses)
    high_usage = [item.name for item in stores if (item.used_percent or 0) >= 85]
    return {
        "datastores": [item.model_dump() for item in stores],
        "total": len(stores),
        "high_usage": high_usage,
    }


async def tasks_recent() -> dict:
    raw = await PbsClient().get("/api2/json/nodes/localhost/tasks?limit=50")
    tasks = normalizers.normalize_tasks(raw)
    failed = [item for item in tasks if item.status and item.status != "OK" and item.status != "running"]
    return {
        "tasks": [item.model_dump() for item in tasks],
        "total": len(tasks),
        "failed": len(failed),
        "failed_task_ids": [item.upid for item in failed[:10]],
    }


async def verify_jobs_health() -> dict:
    raw = await PbsClient().get("/api2/json/config/verify")
    jobs = normalizers.normalize_verify_jobs(raw)
    disabled = [item.id for item in jobs if item.disabled]
    failed = [item.id for item in jobs if item.last_run_status and item.last_run_status != "OK"]
    return {
        "verify_jobs": [item.model_dump() for item in jobs],
        "total": len(jobs),
        "disabled": disabled,
        "failed": failed,
    }


async def backup_jobs_health() -> dict:
    client = PbsClient()
    stores_raw = await client.get("/api2/json/admin/datastore")
    stores = [
        str(item.get("store") or item.get("name") or "")
        for item in stores_raw or []
        if isinstance(item, dict) and (item.get("store") or item.get("name"))
    ]
    groups = []
    for store in stores:
        snapshots = await client.get(f"/api2/json/admin/datastore/{store}/snapshots")
        groups.extend(normalizers.normalize_backup_groups(store, snapshots))
    return {
        "backup_groups": [item.model_dump() for item in groups],
        "groups_total": len(groups),
        "stores_total": len(stores),
    }


async def summary() -> dict:
    health, datastores, tasks, verify, backups = await asyncio.gather(
        PbsProvider().health(),
        _safe(datastores_status()),
        _safe(tasks_recent()),
        _safe(verify_jobs_health()),
        _safe(backup_jobs_health()),
    )
    findings: list[dict[str, str]] = []
    if health.status != "healthy":
        findings.append({"severity": "critical" if health.status in {"unreachable", "misconfigured", "unavailable"} else "warning", "message": health.detail or health.status})
    if datastores.get("high_usage"):
        findings.append({"severity": "critical", "message": f"PBS datastore usage high: {', '.join(datastores['high_usage'][:4])}"})
    if tasks.get("failed"):
        findings.append({"severity": "warning", "message": f"{tasks['failed']} recent PBS task(s) failed"})
    if verify.get("failed"):
        findings.append({"severity": "warning", "message": f"{len(verify['failed'])} PBS verify job(s) failed"})
    if verify.get("total") == 0 and "unavailable" not in verify:
        findings.append(
            {
                "severity": "warning",
                "message": "PBS has no verify jobs configured",
                "code": "verify_jobs_missing",
            }
        )
    if not backups.get("groups_total") and "unavailable" not in backups:
        findings.append({"severity": "warning", "message": "PBS has no backup groups visible"})
    backup_groups = backups.get("backup_groups", [])
    backup_max_age_days = _positive_float(
        provider_config("pbs").get("backup_group_max_age_days"), 3.0
    )
    ignored_groups = {
        str(item).strip() for item in provider_config("pbs").get("backup_ignore_groups", [])
    }
    now = datetime.now(UTC).timestamp()
    backup_ages = []
    stale_groups = []
    for group in backup_groups if isinstance(backup_groups, list) else []:
        if not isinstance(group, dict):
            continue
        store = str(group.get("store") or "").strip()
        group_key = f"{group.get('backup_type')}/{group.get('backup_id')}"
        if group_key in ignored_groups or f"{store}:{group_key}" in ignored_groups:
            continue
        latest = group.get("latest_backup_at")
        if not isinstance(latest, (int, float)) or latest <= 0:
            continue
        age_days = (now - float(latest)) / 86400
        backup_ages.append(age_days)
        if age_days > backup_max_age_days:
            stale_groups.append((store, group_key, age_days))
    if stale_groups:
        findings.append(
            {
                "severity": "critical"
                if any(age > backup_max_age_days * 2 for _, _, age in stale_groups)
                else "warning",
                "message": (
                    f"{len(stale_groups)} PBS backup group(s) older than "
                    f"{backup_max_age_days:g} day(s)"
                ),
                "code": "backup_groups_stale",
            }
        )
    status = health.status
    if status == "healthy" and findings:
        status = "degraded"
    return {
        "summary": {
            "provider_id": "pbs",
            "status": status,
            "severity": _severity(status, findings),
            "generated_at": datetime.now(UTC).isoformat(),
            "metrics": {
                "datastores_total": datastores.get("total", 0),
                "datastores_high_usage": len(datastores.get("high_usage", [])),
                "recent_tasks_failed": tasks.get("failed", 0),
                "verify_jobs_total": verify.get("total", 0),
                "backup_groups_total": backups.get("groups_total", 0),
                "backup_groups_stale": len(stale_groups),
                "backup_oldest_age_days": round(max(backup_ages), 1) if backup_ages else None,
                "backup_max_age_days": backup_max_age_days,
            },
            "findings": findings[:12],
            "next_actions": [item["message"] for item in findings[:5]],
        }
    }


async def _safe(coro) -> dict:
    try:
        return await coro
    except ProviderError as exc:
        return {"unavailable": exc.code, "detail": exc.message}
    except Exception as exc:
        return {"unavailable": "error", "detail": exc.__class__.__name__}


def _severity(status: str, findings: list[dict[str, str]]) -> str:
    if status in {"unreachable", "misconfigured", "unavailable"}:
        return "critical"
    if any(item["severity"] == "critical" for item in findings):
        return "critical"
    if status in {"degraded", "unknown"} or any(item["severity"] == "warning" for item in findings):
        return "warning"
    return "ok"


def _positive_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
