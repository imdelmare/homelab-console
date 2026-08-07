"""Authored runbooks: ordered, tool-referencing procedures per incident_type.

Loaded from `runbooks_config_path` (a YAML file kept separate from the
homelab inventory/dependency-graph config, since runbooks are bulkier
authored content, not topology). Replaces task_context.py's flat
PROVIDER_TOOL_ALLOWLIST/KEYWORD_TOOL_ALLOWLIST merge with an explicit,
ordered, editable-without-redeploy procedure for the incident types that
have one; incident types with no runbook keep the flat-allowlist fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.settings import get_settings
from app.services.inventory import _project_path
from app.tools.registry import get_tool

logger = logging.getLogger("homelab.runbooks")


@dataclass(frozen=True)
class RunbookStep:
    tool_id: str
    evidence: str = ""


@dataclass(frozen=True)
class Runbook:
    incident_type: str
    label: str
    steps: list[RunbookStep]
    escalation_note: str = ""


def config_path() -> Path:
    return _project_path(get_settings().runbooks_config_path)


@lru_cache
def _load_raw() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def clear_cache() -> None:
    _load_raw.cache_clear()
    _runbooks.cache_clear()


@lru_cache
def _runbooks() -> dict[str, Runbook]:
    result: dict[str, Runbook] = {}
    for item in _load_raw().get("runbooks", []) or []:
        incident_type = str(item.get("incident_type") or "")
        if not incident_type:
            continue
        steps = []
        for raw_step in item.get("steps", []) or []:
            tool_id = str(raw_step.get("tool_id") or "")
            tool = get_tool(tool_id)
            if tool is None or not tool.enabled or tool.mode != "read":
                logger.warning(
                    "runbook %s references unusable tool_id %r, dropping step",
                    incident_type,
                    tool_id,
                )
                continue
            steps.append(RunbookStep(tool_id=tool_id, evidence=str(raw_step.get("evidence") or "")))
        if not steps:
            logger.warning("runbook %s has no valid steps after filtering, ignoring", incident_type)
            continue
        result[incident_type] = Runbook(
            incident_type=incident_type,
            label=str(item.get("label") or incident_type),
            steps=steps,
            escalation_note=str(item.get("escalation_note") or ""),
        )
    return result


def list_runbooks() -> list[Runbook]:
    return list(_runbooks().values())


def get_runbook(incident_type: str) -> Runbook | None:
    return _runbooks().get(incident_type)
