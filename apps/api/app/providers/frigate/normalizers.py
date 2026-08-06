"""Convert raw Frigate API payloads into normalized internal models."""

from typing import Any

from app.providers.frigate.models import (
    CameraConfig,
    CameraStats,
    ConfigSummary,
    DetectorStats,
    EventInfo,
    ReviewInfo,
    ServiceStats,
)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize_service_stats(raw: Any) -> ServiceStats:
    raw = _dict(raw)
    service = _dict(raw.get("service"))
    return ServiceStats(
        detection_fps=raw.get("detection_fps"),
        process_uptime=raw.get("process_uptime"),
        uptime_seconds=service.get("uptime"),
        version=str(service.get("version", "") or ""),
        latest_version=str(service.get("latest_version", "") or ""),
        temperatures={
            str(name): float(value)
            for name, value in _dict(service.get("temperatures")).items()
            if isinstance(value, (int, float))
        },
    )


def normalize_camera_stats(raw: Any) -> list[CameraStats]:
    cameras = []
    for name, camera in sorted(_dict(_dict(raw).get("cameras")).items()):
        if not isinstance(camera, dict):
            continue
        cameras.append(
            CameraStats(
                name=str(name),
                camera_fps=camera.get("camera_fps"),
                detection_fps=camera.get("detection_fps"),
                process_fps=camera.get("process_fps"),
                skipped_fps=camera.get("skipped_fps"),
            )
        )
    return cameras


def normalize_detector_stats(raw: Any) -> list[DetectorStats]:
    detectors = []
    for name, detector in sorted(_dict(_dict(raw).get("detectors")).items()):
        if not isinstance(detector, dict):
            continue
        detectors.append(
            DetectorStats(
                name=str(name),
                inference_speed=detector.get("inference_speed"),
                detection_start=detector.get("detection_start"),
                pid=detector.get("pid"),
            )
        )
    return detectors


def normalize_camera_config(name: str, config: dict[str, Any], stats: Any) -> CameraConfig:
    detect = _dict(config.get("detect"))
    record = _dict(config.get("record"))
    snapshots = _dict(config.get("snapshots"))
    zones = _dict(config.get("zones"))
    camera_stats = _dict(_dict(_dict(stats).get("cameras")).get(name))
    return CameraConfig(
        name=str(name),
        enabled=config.get("enabled", True),
        detect_enabled=detect.get("enabled", True),
        detect_fps=detect.get("fps"),
        record_enabled=record.get("enabled"),
        snapshots_enabled=snapshots.get("enabled"),
        zones=sorted(str(zone) for zone in zones),
        camera_fps=camera_stats.get("camera_fps"),
        detection_fps=camera_stats.get("detection_fps"),
        process_fps=camera_stats.get("process_fps"),
        skipped_fps=camera_stats.get("skipped_fps"),
    )


def normalize_camera_configs(config_payload: Any, stats_payload: Any) -> list[CameraConfig]:
    return [
        normalize_camera_config(name, config, stats_payload)
        for name, config in sorted(_dict(_dict(config_payload).get("cameras")).items())
        if isinstance(config, dict)
    ]


def normalize_config_summary(raw: Any, cameras_total: int) -> ConfigSummary:
    raw = _dict(raw)
    return ConfigSummary(
        version=str(raw.get("version", "") or ""),
        safe_mode=raw.get("safe_mode"),
        mqtt_enabled=bool(_dict(raw.get("mqtt")).get("enabled")),
        record_enabled=_dict(raw.get("record")).get("enabled"),
        snapshots_enabled=_dict(raw.get("snapshots")).get("enabled"),
        cameras_total=cameras_total,
    )


def normalize_events(raw: Any) -> list[EventInfo]:
    events = []
    for item in _list(raw):
        if not isinstance(item, dict):
            continue
        sub_label = item.get("sub_label")
        events.append(
            EventInfo(
                id=str(item.get("id", "")),
                camera=str(item.get("camera", "")),
                label=str(item.get("label", "")),
                sub_label=str(sub_label) if sub_label is not None else None,
                start_time=item.get("start_time"),
                end_time=item.get("end_time"),
                score=item.get("score"),
                top_score=item.get("top_score"),
                has_clip=item.get("has_clip"),
                has_snapshot=item.get("has_snapshot"),
                false_positive=item.get("false_positive"),
                zones=[str(zone) for zone in _list(item.get("zones"))],
            )
        )
    return events


def normalize_reviews(raw: Any) -> list[ReviewInfo]:
    reviews = []
    for item in _list(raw):
        if not isinstance(item, dict):
            continue
        data = _dict(item.get("data"))
        detections = _list(data.get("detections"))
        reviews.append(
            ReviewInfo(
                id=str(item.get("id", "")),
                camera=str(item.get("camera", "")),
                severity=str(item.get("severity", "")),
                start_time=item.get("start_time"),
                end_time=item.get("end_time"),
                thumb_path=str(item.get("thumb_path", "")),
                detections=len(detections),
                objects=sorted(
                    {
                        str(event.get("label"))
                        for event in detections
                        if isinstance(event, dict) and event.get("label")
                    }
                ),
                zones=[str(zone) for zone in _list(data.get("zones"))],
            )
        )
    return reviews
