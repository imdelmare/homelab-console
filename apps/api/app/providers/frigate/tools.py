"""Frigate provider and read-only tool implementations."""

from datetime import UTC, datetime
from typing import Any

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.frigate import normalizers
from app.providers.frigate.client import FrigateClient
from app.providers.httpclient import HEALTH_STATUS_MAP


class FrigateProvider(Provider):
    id = "frigate"
    display_name = "Frigate"
    credential_requirements = ("frigate.base_url",)

    def client(self) -> FrigateClient:
        return FrigateClient()

    def ready(self) -> bool:
        return self.client().is_configured()

    async def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        client = self.client()
        if not client.is_configured():
            return ProviderHealth(
                provider_id=self.id,
                status="unavailable",
                detail="not configured",
                checked_at=now,
            )
        try:
            await client.get("/api/version", timeout=4.0, response_mode="auto")
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _format_duration(seconds: Any) -> str | None:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    total_minutes = int(seconds) // 60
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    return f"{days}d {hours}h {minutes}m"


async def version() -> dict:
    raw = await FrigateClient().get("/api/version", response_mode="auto")
    return {"version": str(raw).strip()}


async def stats() -> dict:
    payload = await FrigateClient().get("/api/stats")
    service = normalizers.normalize_service_stats(payload).model_dump()
    cameras = [item.model_dump() for item in normalizers.normalize_camera_stats(payload)]
    detectors = [item.model_dump() for item in normalizers.normalize_detector_stats(payload)]
    process_fps_values = [
        float(item["process_fps"])
        for item in cameras
        if isinstance(item.get("process_fps"), (int, float))
    ]
    detection_fps_values = [
        float(item["detection_fps"])
        for item in cameras
        if isinstance(item.get("detection_fps"), (int, float))
    ]
    process_fps_total = round(sum(process_fps_values), 2)
    detection_fps_total = service.get("detection_fps")
    if not isinstance(detection_fps_total, (int, float)):
        detection_fps_total = round(sum(detection_fps_values), 2)
    return {
        "metrics": {
            "cameras_total": len(cameras),
            "uptime_seconds": service.get("uptime_seconds"),
            "uptime_human": _format_duration(service.get("uptime_seconds")),
            "detection_fps_total": detection_fps_total,
            "process_fps_total": process_fps_total,
            "process_fps_average": (
                round(process_fps_total / len(process_fps_values), 2)
                if process_fps_values
                else None
            ),
        },
        "service": service,
        "cameras": cameras,
        "detectors": detectors,
    }


async def config_summary() -> dict:
    payload = await FrigateClient().get("/api/config")
    stats_payload = await FrigateClient().get("/api/stats")
    cameras = normalizers.normalize_camera_configs(payload, stats_payload)
    return {
        "config": normalizers.normalize_config_summary(payload, len(cameras)).model_dump(),
        "cameras": [item.model_dump() for item in cameras],
    }


async def cameras_list() -> dict:
    payload = await config_summary()
    return {"cameras": payload["cameras"], "total": len(payload["cameras"])}


async def events_recent(limit: int = 20) -> dict:
    events = normalizers.normalize_events(await FrigateClient().get(f"/api/events?limit={limit}"))
    return {"events": [item.model_dump() for item in events], "total": len(events)}


async def review_recent(limit: int = 20) -> dict:
    reviews = normalizers.normalize_reviews(await FrigateClient().get(f"/api/review?limit={limit}"))
    return {"reviews": [item.model_dump() for item in reviews], "total": len(reviews)}


async def sub_labels() -> dict:
    labels = [str(item) for item in _list(await FrigateClient().get("/api/sub_labels"))]
    return {"sub_labels": labels, "total": len(labels)}
