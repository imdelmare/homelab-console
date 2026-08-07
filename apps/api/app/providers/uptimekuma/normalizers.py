"""Convert raw Uptime Kuma payloads (Prometheus /metrics text and the
status-page heartbeat JSON) into normalized internal models."""

import re
from typing import Any

from app.providers.uptimekuma.models import HeartbeatMonitor, MonitorStatus

_STATUS_LABELS = {0: "down", 1: "up", 2: "pending", 3: "maintenance"}

_METRIC_LINE = re.compile(r'^monitor_status\{(?P<labels>[^}]*)\}\s+(?P<value>\d+)')
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _status(value: Any) -> str:
    return _STATUS_LABELS.get(value, "unknown") if isinstance(value, int) else "unknown"


def parse_monitor_metrics(text: str) -> list[MonitorStatus]:
    monitors = []
    for line in text.splitlines():
        match = _METRIC_LINE.match(line.strip())
        if not match:
            continue
        labels = {key: value for key, value in _LABEL.findall(match.group("labels"))}
        value = int(match.group("value"))
        monitors.append(
            MonitorStatus(
                name=labels.get("monitor_name", ""),
                type=labels.get("monitor_type", ""),
                target=_monitor_target(labels),
                status=_STATUS_LABELS.get(value, str(value)),
            )
        )
    return monitors


def _monitor_target(labels: dict[str, str]) -> str:
    url = labels.get("monitor_url", "")
    hostname = labels.get("monitor_hostname", "")
    port = labels.get("monitor_port", "")
    if url and url not in {"http://", "https://", "null"}:
        return url
    if hostname and hostname != "null":
        if port and port != "null":
            return f"{hostname}:{port}"
        return hostname
    return url if url != "null" else ""


def normalize_heartbeats(raw: dict[str, Any]) -> list[HeartbeatMonitor]:
    heartbeats = _dict(raw.get("heartbeatList"))
    uptimes = _dict(raw.get("uptimeList"))
    monitors = []
    for monitor_id, beats in heartbeats.items():
        latest = _dict(beats[-1] if isinstance(beats, list) and beats else None)
        monitor_key = str(monitor_id)
        monitors.append(
            HeartbeatMonitor(
                monitor_id=monitor_key,
                status=_status(latest.get("status")),
                last_ping_ms=latest.get("ping"),
                last_time=latest.get("time", ""),
                uptime_24h=uptimes.get(f"{monitor_key}_24"),
            )
        )
    return monitors
