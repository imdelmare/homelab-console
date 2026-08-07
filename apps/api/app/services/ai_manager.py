"""Narrow client for the inventory-declared LAN AI manager."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.settings import get_settings
from app.services.inventory import get_host
from app.services.redaction import redact


class AIManagerError(Exception):
    def __init__(self, message: str, *, telemetry: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.telemetry = telemetry or {}


class AIManagerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_text: str
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    telemetry: dict[str, Any] = Field(default_factory=dict)


_unavailable_until = 0.0
_inference_slot = asyncio.Semaphore(1)


def ai_manager_configured() -> bool:
    try:
        _endpoint()
    except AIManagerError:
        return False
    return bool(get_settings().ai_manager_model)


def ai_manager_available() -> bool:
    return time.monotonic() >= _unavailable_until


def mark_ai_manager_available() -> None:
    global _unavailable_until
    _unavailable_until = 0.0


def mark_ai_manager_unavailable() -> None:
    global _unavailable_until
    _unavailable_until = time.monotonic() + get_settings().ai_manager_failure_cooldown_seconds


async def request_ai_manager(
    *,
    instructions: str,
    context: dict[str, Any],
    schema_name: str,
    schema: dict[str, Any],
    max_output_tokens: int,
    timeout_seconds: float | None = None,
    prompt_version: str = "v1",
    schema_version: str = "v1",
) -> AIManagerResult:
    settings = get_settings()
    if not settings.ai_manager_model:
        raise AIManagerError("AI manager model is not configured")

    payload = {
        "model": settings.ai_manager_model,
        "messages": [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": json.dumps(redact(context), ensure_ascii=False, default=str),
            },
        ],
        "temperature": 0,
        "max_tokens": max_output_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema,
            },
        },
    }
    timeout = httpx.Timeout(
        timeout_seconds or settings.ai_manager_timeout_seconds,
        connect=settings.ai_manager_connect_timeout_seconds,
    )
    queue_started = time.perf_counter()
    await _inference_slot.acquire()
    queue_wait_ms = max(0, round((time.perf_counter() - queue_started) * 1000))
    inference_started = time.perf_counter()

    def telemetry(error_kind: str = "") -> dict[str, Any]:
        return {
            "provider": "ai_manager",
            "fallback_used": False,
            "fallback_reason": "",
            "error_kind": error_kind,
            "queue_wait_ms": queue_wait_ms,
            "inference_latency_ms": max(0, round((time.perf_counter() - inference_started) * 1000)),
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "model_version": settings.ai_manager_model,
        }

    try:
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.post(
                    _endpoint(),
                    headers={"Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise AIManagerError("AI manager timed out", telemetry=telemetry("timeout")) from exc
        except httpx.HTTPError as exc:
            raise AIManagerError("AI manager request failed", telemetry=telemetry("transport")) from exc

        if response.status_code >= 400:
            raise AIManagerError(
                f"AI manager returned HTTP {response.status_code}",
                telemetry=telemetry("http"),
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AIManagerError(
                "AI manager returned an invalid response",
                telemetry=telemetry("invalid_json"),
            ) from exc
        if not isinstance(body, dict):
            raise AIManagerError(
                "AI manager returned an invalid response",
                telemetry=telemetry("invalid_response"),
            )

        choices = body.get("choices")
        message = (
            choices[0].get("message")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else None
        )
        output_text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(output_text, str):
            raise AIManagerError(
                "AI manager returned an invalid response",
                telemetry=telemetry("invalid_response"),
            )

        usage_value = body.get("usage")
        usage: dict[str, Any] = usage_value if isinstance(usage_value, dict) else {}
        details_value = usage.get("prompt_tokens_details")
        details: dict[str, Any] = details_value if isinstance(details_value, dict) else {}
        try:
            input_tokens = _usage_count(usage.get("prompt_tokens"))
            cached_input_tokens = _usage_count(details.get("cached_tokens"))
            output_tokens = _usage_count(usage.get("completion_tokens"))
        except (TypeError, ValueError) as exc:
            raise AIManagerError(
                "AI manager returned invalid usage data",
                telemetry=telemetry("invalid_response"),
            ) from exc
        return AIManagerResult(
            output_text=output_text,
            model=f"ai-manager:{body.get('model') or settings.ai_manager_model}",
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            telemetry=telemetry(),
        )
    finally:
        _inference_slot.release()


def _endpoint() -> str:
    settings = get_settings()
    host = get_host(settings.ai_manager_host_id)
    if host is None or not host.address:
        raise AIManagerError("AI manager host is not declared in inventory")
    try:
        address = ipaddress.ip_address(host.address)
    except ValueError as exc:
        raise AIManagerError("AI manager inventory address must be an IP address") from exc
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        raise AIManagerError("AI manager inventory address must be private")
    if settings.ai_manager_port not in host.check_ports:
        raise AIManagerError("AI manager port is not declared in inventory")
    return f"http://{address}:{settings.ai_manager_port}/v1/chat/completions"


def _usage_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
        raise ValueError("usage count must be an integer")
    count = int(value)
    if count < 0:
        raise ValueError("usage count must be non-negative")
    return count
