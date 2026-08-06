"""Provider secret loading from the local secrets file and credentials env.

Secret values never leave this module except inside provider clients; they
must never be logged, audited, or returned by any endpoint.
"""

from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from app.core.settings import get_settings


def _project_path(path: str) -> Path:
    """See the identical helper in app.services.inventory for why this
    short-circuits absolute paths (always the case in a real deployment)
    instead of relying on a parent-directory depth that doesn't hold in
    the container's flattened /app/app tree."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    parents = Path(__file__).resolve().parents
    root = parents[4] if len(parents) > 4 else parents[-1]
    return (root / candidate).resolve()


def load_secrets() -> dict[str, Any]:
    settings = get_settings()
    path = _project_path(settings.secrets_path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_credentials_env() -> dict[str, str]:
    """Legacy local credentials env file (gitignored), used as fallback."""
    path = _project_path("config/credentials.local.env")
    if not path.exists():
        return {}
    return {key: str(value) for key, value in dotenv_values(path).items() if value is not None}


def get_provider_secrets(provider_id: str) -> dict[str, Any]:
    return load_secrets().get(provider_id) or {}
