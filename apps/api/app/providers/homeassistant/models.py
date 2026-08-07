"""Normalized Home Assistant models. These are the only shapes exposed to the
frontend and to model providers — never the raw vendor response."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ApiStatus(_Model):
    message: str = ""


class UnitSystem(_Model):
    length: str = ""
    mass: str = ""
    temperature: str = ""
    volume: str = ""


class InstanceConfig(_Model):
    version: str = ""
    location_name: str = ""
    time_zone: str = ""
    state: str = ""
    unit_system: UnitSystem = UnitSystem()
    components_count: int = 0


class EntityState(_Model):
    entity_id: str = ""
    domain: str = ""
    state: str = ""
    friendly_name: str = ""
    last_changed: str = ""
    last_updated: str = ""


class StatesSummary(_Model):
    entities_total: int = 0
    domains: dict[str, int] = {}
    problem_entities: int = 0


class ServiceDomain(_Model):
    domain: str = ""
    services: list[str] = []
    count: int = 0


class LogbookEvent(_Model):
    when: str = ""
    name: str = ""
    entity_id: str = ""
    domain: str = ""
    state: str = ""
    message: str = ""
