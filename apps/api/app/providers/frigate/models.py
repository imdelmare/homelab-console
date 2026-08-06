"""Normalized Frigate models. These are the only shapes exposed to the
frontend and to model providers — never the raw vendor response."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ServiceStats(_Model):
    detection_fps: float | None = None
    process_uptime: int | None = None
    uptime_seconds: int | None = None
    version: str = ""
    latest_version: str = ""
    # Detector hardware temperatures (e.g. Coral PCIe/M.2 "apex_0"), °C.
    temperatures: dict[str, float] = {}


class CameraStats(_Model):
    name: str
    camera_fps: float | None = None
    detection_fps: float | None = None
    process_fps: float | None = None
    skipped_fps: float | None = None


class DetectorStats(_Model):
    name: str
    inference_speed: float | None = None
    detection_start: float | None = None
    pid: int | None = None


class CameraConfig(_Model):
    name: str
    enabled: bool | None = True
    detect_enabled: bool | None = True
    detect_fps: float | None = None
    record_enabled: bool | None = None
    snapshots_enabled: bool | None = None
    zones: list[str] = []
    camera_fps: float | None = None
    detection_fps: float | None = None
    process_fps: float | None = None
    skipped_fps: float | None = None


class ConfigSummary(_Model):
    version: str = ""
    safe_mode: bool | None = None
    mqtt_enabled: bool = False
    record_enabled: bool | None = None
    snapshots_enabled: bool | None = None
    cameras_total: int = 0


class EventInfo(_Model):
    id: str = ""
    camera: str = ""
    label: str = ""
    sub_label: str | None = None
    start_time: float | None = None
    end_time: float | None = None
    score: float | None = None
    top_score: float | None = None
    has_clip: bool | None = None
    has_snapshot: bool | None = None
    false_positive: bool | None = None
    zones: list[str] = []


class ReviewInfo(_Model):
    id: str = ""
    camera: str = ""
    severity: str = ""
    start_time: float | None = None
    end_time: float | None = None
    thumb_path: str = ""
    detections: int = 0
    objects: list[str] = []
    zones: list[str] = []
