"""Convert raw EMQX API payloads into normalized internal models."""

from typing import Any

from app.providers.emqx.models import EmqxNode, EmqxStats


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_nodes(raw: Any) -> list[EmqxNode]:
    nodes = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        nodes.append(
            EmqxNode(
                node=item.get("node", ""),
                status=item.get("node_status", ""),
                version=item.get("version", ""),
                uptime_ms=item.get("uptime"),
                connections=item.get("connections"),
                memory_used=item.get("memory_used"),
                memory_total=item.get("memory_total"),
                load1=item.get("load1"),
            )
        )
    return nodes


def normalize_stats(entries: list[Any]) -> EmqxStats:
    """Sum per-node numeric counters into one cluster-wide view."""
    merged: dict[str, Any] = {}
    for entry in entries:
        for key, value in _dict(entry).items():
            if isinstance(value, (int, float)):
                merged[key] = merged.get(key, 0) + value
    return EmqxStats(
        connections=merged.get("connections.count"),
        connections_max=merged.get("connections.max"),
        live_connections=merged.get("live_connections.count"),
        sessions=merged.get("sessions.count"),
        subscriptions=merged.get("subscriptions.count"),
        topics=merged.get("topics.count"),
        retained_messages=merged.get("retained.count"),
    )
