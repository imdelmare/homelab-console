"""Network UPS Tools provider and read-only tool implementations."""

from __future__ import annotations

from datetime import UTC, datetime

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.httpclient import HEALTH_STATUS_MAP
from app.providers.nutups import normalizers
from app.providers.nutups.client import NutUpsClient


class NutUpsProvider(Provider):
    id = "nutups"
    display_name = "NUT UPS"
    credential_requirements = ("nutups.host",)

    def client(self) -> NutUpsClient:
        return NutUpsClient()

    def ready(self) -> bool:
        return self.client().is_configured()

    async def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        client = self.client()
        if not client.is_configured():
            return ProviderHealth(provider_id=self.id, status="unavailable", detail="not configured", checked_at=now)
        try:
            variables = await client.variables()
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        status = normalizers.normalize_status(client.ups_name or "default", variables)
        if status.status in {"on_battery", "low_battery", "replace_battery", "alarm"}:
            return ProviderHealth(
                provider_id=self.id,
                status="degraded",
                detail=f"UPS status is {status.status}",
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def devices_list() -> dict:
    devices = normalizers.normalize_devices(await NutUpsClient().list_ups())
    return {"devices": [item.model_dump() for item in devices], "total": len(devices)}


async def status() -> dict:
    client = NutUpsClient()
    ups_name = client.ups_name or await client.default_ups_name()
    variables = await client.variables(ups_name)
    return {"ups": normalizers.normalize_status(ups_name, variables).model_dump()}


async def summary() -> dict:
    health = await NutUpsProvider().health()
    findings: list[dict[str, str]] = []
    metrics = {
        "battery_charge_percent": None,
        "battery_runtime_seconds": None,
        "load_percent": None,
        "status": "unknown",
    }
    try:
        ups = (await status())["ups"]
    except ProviderError as exc:
        ups = {}
        findings.append({
            "severity": "critical" if exc.code in {"configuration_missing", "unreachable", "auth_failed"} else "warning",
            "message": exc.message,
        })
    if ups:
        metrics = {
            "battery_charge_percent": ups.get("battery_charge_percent"),
            "battery_runtime_seconds": ups.get("battery_runtime_seconds"),
            "load_percent": ups.get("load_percent"),
            "status": ups.get("status", "unknown"),
        }
        if ups.get("status") in {"on_battery", "low_battery"}:
            findings.append({"severity": "critical", "message": f"UPS is {ups['status']}"})
        elif ups.get("status") in {"replace_battery", "alarm"}:
            findings.append({"severity": "warning", "message": f"UPS reports {ups['status']}"})
        charge = ups.get("battery_charge_percent")
        if isinstance(charge, (int, float)) and charge < 40:
            findings.append({"severity": "warning", "message": f"UPS battery charge is low ({charge}%)"})
        runtime = ups.get("battery_runtime_seconds")
        if isinstance(runtime, (int, float)) and runtime < 600:
            findings.append({"severity": "warning", "message": "UPS runtime is below 10 minutes"})
    status_value = health.status
    if status_value == "healthy" and findings:
        status_value = "degraded"
    return {
        "summary": {
            "provider_id": "nutups",
            "status": status_value,
            "severity": _severity(status_value, findings),
            "generated_at": datetime.now(UTC).isoformat(),
            "metrics": metrics,
            "findings": findings[:12],
            "next_actions": [item["message"] for item in findings[:5]],
        }
    }


def _severity(status_value: str, findings: list[dict[str, str]]) -> str:
    if status_value in {"unreachable", "misconfigured", "unavailable"}:
        return "critical"
    if any(item["severity"] == "critical" for item in findings):
        return "critical"
    if status_value in {"degraded", "unknown"} or any(item["severity"] == "warning" for item in findings):
        return "warning"
    return "ok"
