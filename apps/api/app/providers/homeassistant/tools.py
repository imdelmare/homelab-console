"""Home Assistant provider and read-only tool implementations."""

from datetime import UTC, datetime, timedelta

from app.providers.base import Provider, ProviderHealth
from app.providers.errors import ProviderError
from app.providers.homeassistant import normalizers
from app.providers.homeassistant.client import HomeAssistantClient
from app.providers.httpclient import HEALTH_STATUS_MAP


class HomeAssistantProvider(Provider):
    id = "homeassistant"
    display_name = "Home Assistant"
    credential_requirements = ("homeassistant.base_url", "homeassistant.token")

    def client(self) -> HomeAssistantClient:
        return HomeAssistantClient()

    def ready(self) -> bool:
        client = self.client()
        return client.is_configured() and client.has_credentials()

    async def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        client = self.client()
        if not client.is_configured():
            return ProviderHealth(
                provider_id=self.id,
                status="unavailable",
                detail="not configured",
                checked_at=now,
            )
        if not client.has_credentials():
            return ProviderHealth(
                provider_id=self.id,
                status="misconfigured",
                detail="token not configured",
                checked_at=now,
            )
        try:
            await client.get("/api/", timeout=4.0)
        except ProviderError as exc:
            return ProviderHealth(
                provider_id=self.id,
                status=HEALTH_STATUS_MAP.get(exc.code, "unknown"),
                detail=exc.message,
                checked_at=now,
            )
        return ProviderHealth(provider_id=self.id, status="healthy", checked_at=now)


async def api_status() -> dict:
    raw = await HomeAssistantClient().get("/api/")
    return {"status": normalizers.normalize_api_status(raw).model_dump()}


async def config() -> dict:
    raw = await HomeAssistantClient().get("/api/config")
    return {"config": normalizers.normalize_config(raw).model_dump()}


async def states_summary() -> dict:
    states = normalizers.normalize_states(await HomeAssistantClient().get("/api/states"))
    problems = [state for state in states if state.state in normalizers.PROBLEM_STATES][:100]
    return {
        "summary": normalizers.summarize_states(states).model_dump(),
        "problem_entities": [state.model_dump() for state in problems],
    }


async def states_list(domain: str | None = None, query: str | None = None, limit: int = 100) -> dict:
    states = normalizers.normalize_states(await HomeAssistantClient().get("/api/states"))
    if domain:
        states = [state for state in states if state.domain == domain]
    if query:
        lowered = query.lower()
        states = [
            state for state in states
            if lowered in state.entity_id.lower() or lowered in state.friendly_name.lower()
        ]
    return {"entities": [state.model_dump() for state in states[:limit]], "total": len(states)}


async def services() -> dict:
    raw = await HomeAssistantClient().get("/api/services")
    return {"domains": [item.model_dump() for item in normalizers.normalize_service_domains(raw)]}


async def error_log_tail(lines: int = 80) -> dict:
    text = await HomeAssistantClient().get("/api/error_log", response_mode="text")
    if not isinstance(text, str):
        text = ""
    rows = [row for row in text.splitlines() if row.strip()]
    return {"lines": rows[-lines:], "total_lines": len(rows)}


async def logbook_recent(hours: int = 2, limit: int = 100) -> dict:
    start = datetime.now(UTC) - timedelta(hours=hours)
    raw = await HomeAssistantClient().get(f"/api/logbook/{start.isoformat()}")
    events = normalizers.normalize_logbook_events(raw)
    return {"events": [event.model_dump() for event in events[:limit]], "total": len(events)}
