"""Coherent, short-lived topology snapshots for the operator console."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Awaitable, Callable

from app.db.models import ProviderConfiguration
from app.db.session import get_session_factory
from app.domain.actors import Actor
from app.providers.registry import get_provider, provider_health_snapshot
from app.services.capability_observations import collect_capability_observations
from app.services.inventory import inventory_status
from app.services.provider_metadata import watcher_ids_for_provider
from app.services.redaction import redact
from app.services.topology import build_topology
from app.services.watchers import incident_public, list_incidents
from app.tools.execution import execute_tool

SNAPSHOT_TTL_SECONDS = 15.0

_snapshot_cache: dict[str, Any] | None = None
_snapshot_expires_at = 0.0
_snapshot_lock = asyncio.Lock()
_last_good: dict[str, tuple[Any, datetime]] = {}


def clear_topology_snapshot_cache() -> None:
    global _snapshot_cache, _snapshot_expires_at
    _snapshot_cache = None
    _snapshot_expires_at = 0.0
    _last_good.clear()


async def _runtime(actor: Actor) -> dict[str, Any]:
    result = await execute_tool("proxmox.topology", {}, actor, source="rest")
    if not result.ok:
        message = result.error.message if result.error else "Proxmox topology failed"
        raise RuntimeError(message)
    return result.result or {}


async def _providers() -> list[dict[str, Any]]:
    async with get_session_factory()() as db:
        snapshots = await provider_health_snapshot(db)
        await db.commit()
        result = []
        for snapshot in snapshots:
            provider = get_provider(snapshot.provider_id)
            configuration = await db.get(
                ProviderConfiguration,
                snapshot.provider_id,
            )
            last_error = None
            if configuration and configuration.last_error_at:
                last_error = {
                    "status": configuration.last_error_status,
                    "message": configuration.last_error_detail,
                    "at": configuration.last_error_at.isoformat(),
                }
            data = snapshot.model_dump(mode="json")
            result.append(
                {
                    "id": snapshot.provider_id,
                    "name": provider.display_name if provider else snapshot.provider_id,
                    "status": data["status"],
                    "detail": data["detail"],
                    "last_ok_at": data["last_ok_at"],
                    "checked_at": data["checked_at"],
                    "tool_count": len(provider.capabilities()) if provider else 0,
                    "watchers": watcher_ids_for_provider(snapshot.provider_id),
                    "last_error": last_error,
                }
            )
        return result


async def _incidents() -> list[dict[str, Any]]:
    async with get_session_factory()() as db:
        return [incident_public(item) for item in await list_incidents(db, status="open", limit=100)]


async def _source(
    name: str,
    collect: Callable[[], Awaitable[Any]],
) -> tuple[Any, dict[str, Any]]:
    now = datetime.now(UTC)
    try:
        value = await collect()
    except Exception as exc:
        previous = _last_good.get(name)
        return (
            deepcopy(previous[0]) if previous else [] if name != "runtime" else {},
            {
                "status": "stale" if previous else "error",
                "observed_at": previous[1].isoformat() if previous else None,
                "error": str(redact(f"{exc.__class__.__name__}: {exc}"))[:500],
            },
        )
    _last_good[name] = (deepcopy(value), now)
    return value, {"status": "fresh", "observed_at": now.isoformat(), "error": ""}


async def _build_snapshot(actor: Actor) -> dict[str, Any]:
    runtime_task = _source("runtime", lambda: _runtime(actor))
    providers_task = _source("providers", _providers)
    observations_task = _source(
        "observations", lambda: collect_capability_observations(actor, source="rest")
    )
    incidents_task = _source("incidents", _incidents)
    (runtime, runtime_meta), (providers, providers_meta), (observations, observations_meta), (
        incidents,
        incidents_meta,
    ) = await asyncio.gather(runtime_task, providers_task, observations_task, incidents_task)

    inventory_meta = inventory_status()
    graph = build_topology(
        runtime or None,
        observation_error=(
            f"Live Proxmox topology {runtime_meta['status']}: {runtime_meta['error']}"
            if runtime_meta["status"] != "fresh"
            else ""
        ),
    )
    warnings = list(graph.warnings)
    if inventory_meta["warning"]:
        warnings.append(f"Inventory {inventory_meta['status']}: {inventory_meta['warning']}")
    graph.warnings = warnings
    generated_at = datetime.now(UTC)
    return {
        "graph": graph.model_dump(mode="json"),
        "providers": providers,
        "observations": observations,
        "incidents": incidents,
        "freshness": {
            "inventory": inventory_meta,
            "runtime": runtime_meta,
            "providers": providers_meta,
            "observations": observations_meta,
            "incidents": incidents_meta,
        },
        "generated_at": generated_at.isoformat(),
        "cache_ttl_seconds": SNAPSHOT_TTL_SECONDS,
    }


async def get_topology_snapshot(actor: Actor, *, force: bool = False) -> dict[str, Any]:
    global _snapshot_cache, _snapshot_expires_at
    if not force and _snapshot_cache is not None and monotonic() < _snapshot_expires_at:
        return deepcopy(_snapshot_cache)
    async with _snapshot_lock:
        if not force and _snapshot_cache is not None and monotonic() < _snapshot_expires_at:
            return deepcopy(_snapshot_cache)
        snapshot = await _build_snapshot(actor)
        _snapshot_cache = snapshot
        _snapshot_expires_at = monotonic() + SNAPSHOT_TTL_SECONDS
        return deepcopy(snapshot)
