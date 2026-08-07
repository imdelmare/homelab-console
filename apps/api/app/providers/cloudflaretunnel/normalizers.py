"""Normalize Cloudflare API envelopes and discard connector identifiers/IPs."""

from typing import Any

from app.providers.cloudflaretunnel.models import ConnectorStatus, TunnelStatus
from app.providers.errors import ProviderError

_STATUS_MAP = {
    "healthy": "healthy",
    "degraded": "degraded",
    "down": "unavailable",
    "inactive": "unavailable",
}


def _result(payload: object) -> Any:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ProviderError(
            "invalid_response", "Cloudflare API returned an unsuccessful response"
        )
    if "result" not in payload:
        raise ProviderError(
            "invalid_response", "Cloudflare API response has no result"
        )
    return payload["result"]


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def normalize_tunnel(payload: object, declared_tunnel_id: str) -> TunnelStatus:
    raw = _result(payload)
    if not isinstance(raw, dict):
        raise ProviderError(
            "invalid_response", "Cloudflare tunnel result is not an object"
        )
    returned_id = str(raw.get("id") or "")
    if returned_id != declared_tunnel_id:
        raise ProviderError(
            "invalid_response", "Cloudflare API returned a different tunnel"
        )
    reported_status = str(raw.get("status") or "").strip().lower()
    normalized_status = _STATUS_MAP.get(reported_status)
    if normalized_status is None:
        raise ProviderError(
            "invalid_response", "Cloudflare API returned an unknown tunnel status"
        )
    config_source = (
        str(raw.get("config_src"))
        if raw.get("config_src") in {"local", "cloudflare"}
        else ""
    )
    return TunnelStatus(
        id=declared_tunnel_id,
        name=str(raw.get("name") or "")[:200],
        status=normalized_status,
        reported_status=reported_status,
        config_source=config_source,
        connected_at=_timestamp(raw.get("conns_active_at")),
        disconnected_at=_timestamp(raw.get("conns_inactive_at")),
    )


def normalize_connectors(
    payload: object, declared_tunnel_id: str
) -> list[ConnectorStatus]:
    raw = _result(payload)
    if not isinstance(raw, list):
        raise ProviderError(
            "invalid_response", "Cloudflare connections result is not a list"
        )
    connectors: list[ConnectorStatus] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        connections = _list(item.get("conns"))
        pending = sum(
            1
            for connection in connections
            if isinstance(connection, dict)
            and connection.get("is_pending_reconnect") is True
        )
        features = _list(item.get("features"))
        connectors.append(
            ConnectorStatus(
                tunnel_id=declared_tunnel_id,
                version=str(item.get("version") or "")[:64],
                architecture=str(item.get("arch") or "")[:32],
                run_at=_timestamp(item.get("run_at")),
                features=[str(feature)[:64] for feature in features[:20]],
                connections_total=len(connections),
                connections_active=max(0, len(connections) - pending),
                connections_pending_reconnect=pending,
            )
        )
    return connectors


def _timestamp(value: object) -> str:
    return str(value)[:64] if isinstance(value, str) else ""
