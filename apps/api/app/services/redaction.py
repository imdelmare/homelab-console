"""Centralized secret redaction for audit events, logs and stored payloads."""

from typing import Any

REDACTED = "[REDACTED]"

# Token usage counters are telemetry, not authentication material. Keep this
# allowlist exact so similarly named credential fields remain protected.
SAFE_TOKEN_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "max_output_tokens",
    }
)

SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "session",
    "otp",
    "csrf",
    "credential",
    "private_key",
    "passphrase",
)

# Provider-specific sensitive keys that do not match the generic fragments.
PROVIDER_SENSITIVE_KEYS: dict[str, tuple[str, ...]] = {
    "proxmox": ("ticket", "csrfpreventiontoken"),
    "homeassistant": ("webhook_id",),
    "telegram": ("callback_data",),
}

_MAX_DEPTH = 12


def is_sensitive_key(key: str, provider_id: str | None = None) -> bool:
    lowered = key.lower()
    if lowered in SAFE_TOKEN_USAGE_KEYS:
        return False
    if any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS):
        return True
    if provider_id:
        extra = PROVIDER_SENSITIVE_KEYS.get(provider_id, ())
        if lowered in extra:
            return True
    return False


def redact(value: Any, provider_id: str | None = None, _depth: int = 0) -> Any:
    """Return a deep copy of ``value`` with sensitive values replaced."""
    if _depth > _MAX_DEPTH:
        return REDACTED

    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if is_sensitive_key(str(key), provider_id)
                else redact(item, provider_id, _depth + 1)
            )
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [redact(item, provider_id, _depth + 1) for item in value]

    return value
