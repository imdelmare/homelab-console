from datetime import UTC, datetime, timedelta

from app.db.models import Incident
from app.core.settings import get_settings
from app.domain.actors import Actor
from app.services.incident_matcher import (
    IncidentMatchDecision,
    OpenCodeGoIncidentMatchModel,
    get_incident_match_model,
    match_incident,
)
from app.services.opencode_go import OpenCodeGoResult
from app.services.tasks_service import create_task
from app.services.watchers import DetectedIncident


OPERATOR = Actor(kind="user", id="operator", label="operator")


def test_incident_matcher_selects_opencode_go(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "task_router_provider", "opencode_go")

    assert isinstance(get_incident_match_model(), OpenCodeGoIncidentMatchModel)


async def test_opencode_go_incident_matcher_contract(monkeypatch):
    captured = {}

    async def fake_request(**kwargs):
        captured.update(kwargs)
        return OpenCodeGoResult(
            output_text=(
                '{"outcome":"new","matched_incident_id":null,"confidence":0.88,'
                '"reason":"different resource","method":"llm","model":"",'
                '"input_tokens":0,"output_tokens":0,"telemetry":{}}'
            ),
            model="deepseek-v4-pro",
            input_tokens=11,
            output_tokens=5,
        )

    monkeypatch.setattr("app.services.incident_matcher.request_structured_decision", fake_request)

    decision = await OpenCodeGoIncidentMatchModel().decide({"candidates": []})

    assert captured["model"] == "deepseek-v4-pro"
    assert decision.outcome == "new"
    assert decision.model == "opencode-go/deepseek-v4-pro"
    assert decision.telemetry["provider"] == "opencode_go"


def detected(*, dedupe_key="gateway:new", severity="warning"):
    return DetectedIncident(
        watcher_id="network.gateway",
        dedupe_key=dedupe_key,
        dedupe_basis="gateway unavailable",
        severity=severity,
        provider_id="opnsense",
        title="WG_DE_GW gateway unavailable",
        description="The WireGuard gateway cannot be reached",
        payload={},
    )


async def resolved_candidate(db, *, dedupe_key="gateway:old"):
    task = await create_task(db, "OPNsense gateway offline", "diagnose", OPERATOR, notify=False)
    now = datetime.now(UTC)
    incident = Incident(
        dedupe_key=dedupe_key,
        watcher_id="network.gateway",
        status="resolved",
        severity="warning",
        provider_id="opnsense",
        title="WG_DE_GW gateway offline",
        description="The WireGuard gateway was unreachable",
        task_id=task.id,
        resolved_at=now - timedelta(minutes=30),
        resolution_reason="signal_cleared",
    )
    db.add(incident)
    await db.flush()
    return incident


class FakeMatcher:
    def __init__(self, incident_id):
        self.incident_id = incident_id

    async def decide(self, context):
        assert len(context["candidates"]) == 1
        return IncidentMatchDecision(
            outcome="already_handled",
            matched_incident_id=self.incident_id,
            confidence=0.96,
            reason="same gateway fault described with different wording",
            method="llm",
            model="fake-luna",
            input_tokens=120,
            output_tokens=20,
        )


async def test_exact_resolved_match_is_deterministic_and_auto_handled(db_session):
    incident = await resolved_candidate(db_session, dedupe_key="gateway:same")

    result = await match_incident(
        db_session,
        detected(dedupe_key="gateway:same"),
        now=datetime.now(UTC),
    )

    assert result.outcome == "already_handled"
    assert result.matched_incident_id == incident.id
    assert result.method == "deterministic"


async def test_ambiguous_match_uses_model_and_validates_candidate(db_session):
    incident = await resolved_candidate(db_session)

    result = await match_incident(
        db_session,
        detected(),
        now=datetime.now(UTC),
        model=FakeMatcher(incident.id),
    )

    assert result.outcome == "already_handled"
    assert result.method == "llm"
    assert result.input_tokens == 120


async def test_model_gate_keeps_deterministic_matching_but_skips_ambiguous_model(
    db_session, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_enabled", False)
    incident = await resolved_candidate(db_session)

    class MustNotRun:
        async def decide(self, _context):
            raise AssertionError("model must not run")

    ambiguous = await match_incident(
        db_session,
        detected(),
        now=datetime.now(UTC),
        model=MustNotRun(),
    )
    exact = await match_incident(
        db_session,
        detected(dedupe_key=incident.dedupe_key),
        now=datetime.now(UTC),
        model=MustNotRun(),
    )

    assert ambiguous.outcome == "new"
    assert ambiguous.reason == "model-assisted matching disabled"
    assert exact.outcome == "already_handled"
    assert exact.method == "deterministic"


async def test_critical_match_always_requires_operator_review(db_session):
    incident = await resolved_candidate(db_session, dedupe_key="gateway:same")

    result = await match_incident(
        db_session,
        detected(dedupe_key="gateway:same", severity="critical"),
        now=datetime.now(UTC),
    )

    assert result.outcome == "possible_match"
    assert result.matched_incident_id == incident.id
