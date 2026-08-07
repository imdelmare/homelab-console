from functools import lru_cache

from app.services import runbooks


def _configure(monkeypatch, raw: dict) -> None:
    # Wrap the replacement in lru_cache too, so it keeps a working
    # .cache_clear() — runbooks.clear_cache() (called both here and by
    # conftest.py's teardown, before monkeypatch has undone this) calls
    # _load_raw.cache_clear() unconditionally.
    monkeypatch.setattr(runbooks, "_load_raw", lru_cache(lambda: raw))
    runbooks.clear_cache()


def test_get_runbook_returns_matching_incident_type(monkeypatch):
    _configure(
        monkeypatch,
        {
            "runbooks": [
                {
                    "incident_type": "gateway_alert",
                    "label": "Gateway",
                    "steps": [{"tool_id": "opnsense.summary", "evidence": "check summary"}],
                    "escalation_note": "ask the operator",
                }
            ]
        },
    )
    runbook = runbooks.get_runbook("gateway_alert")
    assert runbook is not None
    assert runbook.label == "Gateway"
    assert runbook.escalation_note == "ask the operator"
    assert [step.tool_id for step in runbook.steps] == ["opnsense.summary"]
    assert runbook.steps[0].evidence == "check summary"


def test_get_runbook_returns_none_for_unknown_incident_type(monkeypatch):
    _configure(monkeypatch, {"runbooks": []})
    assert runbooks.get_runbook("gateway_alert") is None


def test_invalid_tool_id_step_is_dropped_not_the_whole_runbook(monkeypatch):
    _configure(
        monkeypatch,
        {
            "runbooks": [
                {
                    "incident_type": "gateway_alert",
                    "label": "Gateway",
                    "steps": [
                        {"tool_id": "totally.fake.tool", "evidence": "nope"},
                        {"tool_id": "opnsense.summary", "evidence": "check summary"},
                    ],
                }
            ]
        },
    )
    runbook = runbooks.get_runbook("gateway_alert")
    assert runbook is not None
    assert [step.tool_id for step in runbook.steps] == ["opnsense.summary"]


def test_runbook_with_no_valid_steps_is_ignored_entirely(monkeypatch):
    _configure(
        monkeypatch,
        {
            "runbooks": [
                {
                    "incident_type": "gateway_alert",
                    "label": "Gateway",
                    "steps": [{"tool_id": "totally.fake.tool", "evidence": "nope"}],
                }
            ]
        },
    )
    assert runbooks.get_runbook("gateway_alert") is None
    assert runbooks.list_runbooks() == []


def test_list_runbooks_returns_all_configured(monkeypatch):
    _configure(
        monkeypatch,
        {
            "runbooks": [
                {
                    "incident_type": "gateway_alert",
                    "label": "Gateway",
                    "steps": [{"tool_id": "opnsense.summary"}],
                },
                {"incident_type": "dns_alert", "label": "DNS", "steps": [{"tool_id": "adguard.summary"}]},
            ]
        },
    )
    assert {runbook.incident_type for runbook in runbooks.list_runbooks()} == {"gateway_alert", "dns_alert"}


def test_missing_config_file_yields_no_runbooks(monkeypatch):
    # RUNBOOKS_CONFIG_PATH points at a deliberately-missing file in tests
    # (see conftest.py's os.environ.update block) — _load_raw() must
    # degrade to an empty dict, not raise.
    runbooks.clear_cache()
    assert runbooks.list_runbooks() == []
    assert runbooks.get_runbook("gateway_alert") is None
