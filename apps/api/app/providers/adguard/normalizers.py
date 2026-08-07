"""Convert raw AdGuard Home API payloads into normalized internal models."""

from typing import Any

from app.providers.adguard.models import (
    AdguardStats,
    AdguardStatus,
    FilteringStatus,
    FilterInfo,
    TopEntry,
)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_status(raw: Any) -> AdguardStatus:
    raw = _dict(raw)
    addresses = raw.get("dns_addresses") if isinstance(raw.get("dns_addresses"), list) else []
    return AdguardStatus(
        version=str(raw.get("version", "")),
        running=raw.get("running"),
        protection_enabled=raw.get("protection_enabled"),
        dns_addresses=[str(address) for address in addresses],
        dns_port=raw.get("dns_port"),
    )


def normalize_top(entries: Any, limit: int = 10) -> list[TopEntry]:
    """AdGuard 'top' lists are [{name: count}, ...]. Flatten and trim."""
    result = []
    for item in entries if isinstance(entries, list) else []:
        if isinstance(item, dict):
            for name, count in item.items():
                result.append(TopEntry(name=str(name), count=count))
    return result[:limit]


def normalize_stats(raw: Any) -> AdguardStats:
    raw = _dict(raw)
    return AdguardStats(
        time_units=str(raw.get("time_units", "")),
        dns_queries=raw.get("num_dns_queries"),
        blocked_filtering=raw.get("num_blocked_filtering"),
        replaced_safebrowsing=raw.get("num_replaced_safebrowsing"),
        replaced_parental=raw.get("num_replaced_parental"),
        avg_processing_time_ms=raw.get("avg_processing_time"),
        top_queried_domains=normalize_top(raw.get("top_queried_domains")),
        top_blocked_domains=normalize_top(raw.get("top_blocked_domains")),
    )


def normalize_filtering_status(raw: Any) -> FilteringStatus:
    raw = _dict(raw)
    filters = []
    for item in raw.get("filters") or []:
        if isinstance(item, dict):
            filters.append(
                FilterInfo(
                    name=str(item.get("name", "")),
                    enabled=item.get("enabled"),
                    rules_count=item.get("rules_count"),
                    last_updated=str(item.get("last_updated", "")),
                )
            )
    return FilteringStatus(
        enabled=raw.get("enabled"),
        update_interval_hours=raw.get("interval"),
        filters_total=len(filters),
        filters=filters,
        user_rules_count=len(raw.get("user_rules") or []),
    )
