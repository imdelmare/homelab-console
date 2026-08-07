"""Async MikroTik client.

RouterOS v7 can be read through the REST API. RouterOS v6 has no REST API,
so this client also supports the read-only RouterOS API socket protocol.
"""

import asyncio
from typing import Any, Literal
from urllib.parse import urlparse

from app.providers.errors import ProviderError
from app.providers.httpclient import BaseJsonClient
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env

_PATH_TO_API_COMMAND = {
    "/rest/system/resource": "/system/resource/print",
    "/rest/system/health": "/system/health/print",
    "/rest/interface": "/interface/print",
    "/rest/interface/lte": "/interface/lte/print",
}


class MikrotikClient(BaseJsonClient):
    provider_id = "mikrotik"

    def __init__(self) -> None:
        super().__init__()
        secrets = get_provider_secrets("mikrotik")
        config = provider_config("mikrotik")
        env = load_credentials_env()

        host = env.get("MIKROTIK_HOST") or ""
        self.base_url = str(
            config.get("base_url")
            or secrets.get("base_url")
            or env.get("MIKROTIK_URL")
            or (f"https://{host}" if host else "")
        ).rstrip("/")
        username = secrets.get("username") or env.get("MIKROTIK_USER") or ""
        password = secrets.get("password") or env.get("MIKROTIK_PASSWORD") or ""
        self.auth = (username, password) if username and password else None
        self.verify_tls = bool(config.get("verify_tls", secrets.get("verify_tls", True)))
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 6.0)))
        self.api_protocol = str(config.get("api_protocol") or secrets.get("api_protocol") or "rest")
        self.api_host = str(config.get("api_host") or secrets.get("api_host") or "")
        self.api_port = int(config.get("api_port") or secrets.get("api_port") or 8728)

    def has_credentials(self) -> bool:
        return self.auth is not None

    def credentials_error(self) -> str:
        return "mikrotik username/password is not configured"

    async def get(
        self,
        path: str,
        timeout: float | None = None,
        *,
        response_mode: Literal["json", "text", "auto"] = "json",
    ) -> Any:
        if self.api_protocol == "routeros":
            return await self._get_routeros(path, timeout=timeout)
        return await super().get(path, timeout=timeout, response_mode=response_mode)

    def _routeros_host(self) -> str:
        if self.api_host:
            return self.api_host
        parsed = urlparse(self.base_url)
        return parsed.hostname or self.base_url.removeprefix("http://").removeprefix("https://").split("/", 1)[0]

    async def _get_routeros(self, path: str, timeout: float | None = None) -> Any:
        if not self.is_configured():
            raise ProviderError("configuration_missing", "mikrotik base_url is not configured")
        if not self.has_credentials():
            raise ProviderError("credentials_missing", self.credentials_error())
        command = _PATH_TO_API_COMMAND.get(path)
        if command is None:
            raise ProviderError("permission_denied", f"mikrotik API path is not allowed: {path}")

        try:
            return await asyncio.wait_for(
                self._run_routeros_command(command),
                timeout or self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise ProviderError("timeout", f"mikrotik did not respond within the timeout ({path})")
        except (ConnectionError, OSError):
            raise ProviderError("unreachable", "mikrotik API socket is unreachable")

    async def _run_routeros_command(self, command: str) -> Any:
        username, password = self.auth or ("", "")
        reader, writer = await asyncio.open_connection(self._routeros_host(), self.api_port)
        try:
            await _write_sentence(writer, ["/login", f"=name={username}", f"=password={password}"])
            login = await _read_reply(reader)
            if login["trap"]:
                raise ProviderError("auth_failed", "mikrotik rejected the credentials")

            await _write_sentence(writer, [command])
            reply = await _read_reply(reader)
            if reply["trap"]:
                raise ProviderError("invalid_response", "mikrotik command failed")
            rows = reply["rows"]
            if command == "/system/resource/print":
                return rows[0] if rows else {}
            return rows
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def _write_sentence(writer: asyncio.StreamWriter, words: list[str]) -> None:
    for word in words:
        encoded = word.encode("utf-8")
        writer.write(_encode_length(len(encoded)) + encoded)
    writer.write(b"\x00")
    await writer.drain()


async def _read_reply(reader: asyncio.StreamReader) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    trap = False
    while True:
        words = await _read_sentence(reader)
        if not words:
            continue
        kind = words[0]
        if kind == "!done":
            return {"rows": rows, "trap": trap}
        if kind == "!trap":
            trap = True
            continue
        if kind == "!re":
            rows.append(_words_to_dict(words[1:]))


async def _read_sentence(reader: asyncio.StreamReader) -> list[str]:
    words = []
    while True:
        length = await _read_length(reader)
        if length == 0:
            return words
        data = await reader.readexactly(length)
        words.append(data.decode("utf-8", errors="replace"))


async def _read_length(reader: asyncio.StreamReader) -> int:
    first = (await reader.readexactly(1))[0]
    if first < 0x80:
        return first
    if first < 0xC0:
        second = (await reader.readexactly(1))[0]
        return ((first & ~0xC0) << 8) + second
    if first < 0xE0:
        rest = await reader.readexactly(2)
        return ((first & ~0xE0) << 16) + (rest[0] << 8) + rest[1]
    if first < 0xF0:
        rest = await reader.readexactly(3)
        return ((first & ~0xF0) << 24) + (rest[0] << 16) + (rest[1] << 8) + rest[2]
    rest = await reader.readexactly(4)
    return (rest[0] << 24) + (rest[1] << 16) + (rest[2] << 8) + rest[3]


def _encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    if length < 0x4000:
        return bytes([(length >> 8) | 0x80, length & 0xFF])
    if length < 0x200000:
        return bytes([(length >> 16) | 0xC0, (length >> 8) & 0xFF, length & 0xFF])
    if length < 0x10000000:
        return bytes([
            (length >> 24) | 0xE0,
            (length >> 16) & 0xFF,
            (length >> 8) & 0xFF,
            length & 0xFF,
        ])
    return bytes([0xF0, (length >> 24) & 0xFF, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])


def _words_to_dict(words: list[str]) -> dict[str, str]:
    row: dict[str, str] = {}
    for word in words:
        if not word.startswith("="):
            continue
        _, key, value = word.split("=", 2)
        row[key] = value
    return row
