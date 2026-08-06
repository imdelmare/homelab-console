"""Shared bounded transport for narrow line-oriented TCP providers.

This module owns connection lifecycle and failure normalization only. Wire
protocol commands and parsers remain in explicit provider drivers; callers can
never use it as a generic TCP passthrough.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TypeVar

from app.providers.errors import ProviderError

T = TypeVar("T")


@dataclass
class TcpTextSession:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    provider_id: str
    max_response_lines: int
    max_response_bytes: int
    lines_read: int = 0
    bytes_read: int = 0

    async def read_line(self) -> str:
        try:
            line = await self.reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise ProviderError(
                "invalid_response", f"{self.provider_id} returned an oversized line"
            ) from exc
        if not line:
            raise ProviderError(
                "invalid_response", f"{self.provider_id} closed the connection unexpectedly"
            )
        self.lines_read += 1
        self.bytes_read += len(line)
        if self.lines_read > self.max_response_lines:
            raise ProviderError(
                "invalid_response", f"{self.provider_id} exceeded the response line limit"
            )
        if self.bytes_read > self.max_response_bytes:
            raise ProviderError(
                "invalid_response", f"{self.provider_id} exceeded the response size limit"
            )
        return line.decode("utf-8", errors="replace").rstrip("\r\n")

    async def write_text(self, payload: str) -> None:
        encoded = payload.encode("utf-8")
        if len(encoded) > 65_536:
            raise ProviderError("invalid_response", "outbound protocol frame is too large")
        self.writer.write(encoded)
        await self.writer.drain()

    async def write_line(self, line: str, *, newline: str = "\n") -> None:
        if "\r" in line or "\n" in line:
            raise ProviderError(
                "configuration_missing", "TCP protocol fields must not contain line breaks"
            )
        await self.write_text(f"{line}{newline}")

    async def close(self) -> None:
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), timeout=1.0)
        except (OSError, asyncio.TimeoutError):
            pass


class BaseTcpTextClient:
    """Connection/error policy shared by explicit TCP text protocol drivers."""

    provider_id: str = ""

    def __init__(self) -> None:
        self.host = ""
        self.port = 0
        self.timeout_seconds = 6.0
        self.max_line_bytes = 65_536
        self.max_response_lines = 10_000
        self.max_response_bytes = 4 * 1024 * 1024

    def is_configured(self) -> bool:
        return bool(self.host and self.port)

    async def execute(
        self,
        operation: str,
        callback: Callable[[], Awaitable[T]],
    ) -> T:
        if not self.is_configured():
            raise ProviderError(
                "configuration_missing", f"{self.provider_id} host is not configured"
            )
        try:
            return await asyncio.wait_for(callback(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise ProviderError(
                "timeout",
                f"{self.provider_id} did not respond within the timeout ({operation})",
            ) from exc
        except ProviderError:
            raise
        except (ConnectionError, OSError) as exc:
            raise ProviderError(
                "unreachable", f"{self.provider_id} TCP endpoint is unreachable"
            ) from exc

    @asynccontextmanager
    async def connection(self):
        reader, writer = await asyncio.open_connection(
            self.host,
            self.port,
            limit=self.max_line_bytes,
        )
        session = TcpTextSession(
            reader=reader,
            writer=writer,
            provider_id=self.provider_id,
            max_response_lines=self.max_response_lines,
            max_response_bytes=self.max_response_bytes,
        )
        try:
            yield session
        finally:
            await session.close()
