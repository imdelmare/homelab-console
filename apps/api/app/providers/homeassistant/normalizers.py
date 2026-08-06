"""Convert raw Home Assistant API payloads into normalized internal models."""

from collections import Counter
from typing import Any

from app.providers.homeassistant.models import (
    ApiStatus,
    EntityState,
    InstanceConfig,
    LogbookEvent,
    ServiceDomain,
    StatesSummary,
    UnitSystem,
)

PROBLEM_STATES = {"unavailable", "unknown"}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_api_status(raw: Any) -> ApiStatus:
    return ApiStatus(message=str(_dict(raw).get("message", "")))


def normalize_config(raw: Any) -> InstanceConfig:
    """Project only safe instance metadata: no coordinates, no internal
    paths or whitelisted directories, no internal/external URLs."""
    raw = _dict(raw)
    unit_system = _dict(raw.get("unit_system"))
    return InstanceConfig(
        version=str(raw.get("version", "")),
        location_name=str(raw.get("location_name", "")),
        time_zone=str(raw.get("time_zone", "")),
        state=str(raw.get("state", "")),
        unit_system=UnitSystem(
            length=str(unit_system.get("length", "")),
            mass=str(unit_system.get("mass", "")),
            temperature=str(unit_system.get("temperature", "")),
            volume=str(unit_system.get("volume", "")),
        ),
        components_count=len(raw.get("components") or []),
    )


def normalize_state(item: dict[str, Any]) -> EntityState:
    attributes = _dict(item.get("attributes"))
    entity_id = str(item.get("entity_id", ""))
    return EntityState(
        entity_id=entity_id,
        domain=entity_id.split(".", 1)[0],
        state=str(item.get("state", "")),
        friendly_name=str(attributes.get("friendly_name", "")),
        last_changed=str(item.get("last_changed", "")),
        last_updated=str(item.get("last_updated", "")),
    )


def normalize_states(raw: Any) -> list[EntityState]:
    items = raw if isinstance(raw, list) else []
    return [normalize_state(item) for item in items if isinstance(item, dict)]


def summarize_states(states: list[EntityState]) -> StatesSummary:
    domains = Counter(state.domain for state in states if state.entity_id)
    return StatesSummary(
        entities_total=len(states),
        domains=dict(sorted(domains.items())),
        problem_entities=len([state for state in states if state.state in PROBLEM_STATES]),
    )


def normalize_service_domains(raw: Any) -> list[ServiceDomain]:
    domains = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        services = item.get("services")
        names = sorted(services.keys()) if isinstance(services, dict) else []
        domains.append(
            ServiceDomain(domain=str(item.get("domain", "")), services=names, count=len(names))
        )
    return domains


def normalize_logbook_events(raw: Any) -> list[LogbookEvent]:
    events = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id", "") or "")
        domain = str(item.get("domain", "") or "") or entity_id.split(".", 1)[0]
        events.append(
            LogbookEvent(
                when=str(item.get("when", "") or ""),
                name=str(item.get("name", "") or ""),
                entity_id=entity_id,
                domain=domain,
                state=str(item.get("state", "") or ""),
                message=str(item.get("message", "") or ""),
            )
        )
    return events
