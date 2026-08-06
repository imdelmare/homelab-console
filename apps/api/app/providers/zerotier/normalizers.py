"""Normalize Legacy Central responses without exposing raw vendor payloads."""

from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any

from app.providers.zerotier.models import ZeroTierMember, ZeroTierNetwork


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_network(raw: Any, declared_network_id: str) -> ZeroTierNetwork:
    item = _dict(raw)
    config = _dict(item.get("config"))
    return ZeroTierNetwork(
        id=declared_network_id,
        name=str(config.get("name") or item.get("name") or "")[:160],
        description=str(config.get("description") or item.get("description") or "")[:500],
        private=(
            bool(config.get("private"))
            if "private" in config
            else bool(item.get("private")) if "private" in item else None
        ),
        members_total=_integer(item.get("totalMemberCount")),
        members_authorized=_integer(item.get("authorizedMemberCount")),
    )


def normalize_members(
    raw: Any,
    declared_network_id: str,
    *,
    offline_after_seconds: float,
    now: datetime | None = None,
) -> list[ZeroTierMember]:
    rows = raw if isinstance(raw, list) else []
    current = now or datetime.now(UTC)
    current_ms = int(current.timestamp() * 1000)
    members: list[ZeroTierMember] = []
    for row in rows:
        item = _dict(row)
        config = _dict(item.get("config"))
        member_id = str(item.get("id") or "").strip().lower()
        if not member_id:
            continue
        last_seen_ms = _integer(item.get("lastSeen"))
        last_seen_at = ""
        if last_seen_ms and last_seen_ms > 0:
            try:
                last_seen_at = datetime.fromtimestamp(
                    last_seen_ms / 1000, tz=UTC
                ).isoformat()
            except (OverflowError, OSError, ValueError):
                last_seen_ms = None
        if last_seen_ms is not None:
            age_ms = max(0, current_ms - last_seen_ms)
            online = age_ms <= int(offline_after_seconds * 1000)
        else:
            online = item.get("online") is True

        raw_ips = config.get("ipAssignments")
        if not isinstance(raw_ips, list):
            raw_ips = item.get("ipAssignments")
        assigned_ips = _normalized_ips(raw_ips)
        members.append(
            ZeroTierMember(
                id=member_id,
                network_id=declared_network_id,
                name=str(item.get("name") or config.get("name") or "")[:160],
                authorized=config.get("authorized") is True,
                online=online,
                stale=not online,
                last_seen_at=last_seen_at,
                assigned_ips=assigned_ips,
            )
        )
    return members


def _normalized_ips(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        candidate = str(item).strip()
        try:
            safe = str(ip_address(candidate))
        except ValueError:
            continue
        if safe not in normalized:
            normalized.append(safe)
    return normalized
