"""Bounded API-lifecycle heartbeat client for External Sentinel v1."""

from __future__ import annotations

import asyncio
import logging
import platform
import socket
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app.core.settings import get_settings
from app.providers.errors import ProviderError
from app.providers.httpclient import BaseJsonClient
from app.services.inventory import SentinelHeartbeatEntry, get_sentinel_heartbeat

logger = logging.getLogger("homelab.sentinel_heartbeat")

HEARTBEAT_INTERVAL_SECONDS = 60
HEARTBEAT_TIMEOUT_SECONDS = 8.0


@dataclass
class HeartbeatState:
    enabled: bool = False
    source_id: str = ""
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    last_error: str = ""


_state = HeartbeatState()


class SentinelHeartbeatClient(BaseJsonClient):
    provider_id = "sentinel_heartbeat"

    def __init__(self, entry: SentinelHeartbeatEntry, token: str) -> None:
        super().__init__()
        self.base_url = entry.base_url
        self.source_id = entry.source_id
        self.token = token
        self.timeout_seconds = HEARTBEAT_TIMEOUT_SECONDS
        self.trust_env = False
        self.headers = {"Authorization": f"Bearer {token}"}

    def has_credentials(self) -> bool:
        return bool(self.token)

    def credentials_error(self) -> str:
        return "Sentinel heartbeat token is not configured"

    async def send(self, payload: dict[str, str]) -> None:
        result = await self._request(
            "POST",
            f"/heartbeat/{quote(self.source_id, safe='')}",
            json_body=payload,
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ProviderError("invalid_response", "Sentinel rejected the heartbeat response")


def validate_configuration() -> tuple[SentinelHeartbeatEntry, str]:
    settings = get_settings()
    if not settings.sentinel_heartbeat_token:
        raise RuntimeError("SENTINEL_HEARTBEAT_TOKEN is required when heartbeat is enabled")
    entry = get_sentinel_heartbeat()
    if entry is None:
        raise RuntimeError(
            "providers.vps.sentinel_heartbeat is required when heartbeat is enabled"
        )
    return entry, settings.sentinel_heartbeat_token


def heartbeat_status() -> dict[str, Any]:
    return asdict(_state)


def reset_state_for_tests() -> None:
    global _state
    _state = HeartbeatState()


async def send_once(client: SentinelHeartbeatClient) -> None:
    now = datetime.now(UTC).isoformat()
    _state.last_attempt_at = now
    payload = {
        "source": "homelab-console-api",
        "sent_at": now,
        "host": socket.gethostname(),
        "platform": platform.system(),
    }
    await client.send(payload)
    _state.last_success_at = datetime.now(UTC).isoformat()
    _state.last_error = ""


async def worker_loop() -> None:
    entry, token = validate_configuration()
    client = SentinelHeartbeatClient(entry, token)
    _state.enabled = True
    _state.source_id = entry.source_id
    logger.info("Sentinel heartbeat worker started source_id=%s", entry.source_id)
    try:
        while True:
            try:
                await send_once(client)
            except asyncio.CancelledError:
                raise
            except ProviderError as exc:
                _state.last_error = exc.code
                logger.warning("Sentinel heartbeat failed code=%s", exc.code)
            except Exception:
                _state.last_error = "internal_error"
                logger.error("Sentinel heartbeat failed code=internal_error")
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
    finally:
        _state.enabled = False
        logger.info("Sentinel heartbeat worker stopped source_id=%s", entry.source_id)
