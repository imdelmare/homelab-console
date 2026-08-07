import asyncio

import pytest

from app.providers.asterisk.client import AsteriskClient
from app.providers.errors import ProviderError
from app.providers.nutups.client import NutUpsClient
from app.providers.tcpclient import BaseTcpTextClient


class ProbeClient(BaseTcpTextClient):
    provider_id = "probe"

    def __init__(self, port: int) -> None:
        super().__init__()
        self.host = "127.0.0.1"
        self.port = port

    async def read_lines(self, count: int) -> list[str]:
        async def operation() -> list[str]:
            async with self.connection() as session:
                return [await session.read_line() for _ in range(count)]

        return await self.execute("read lines", operation)


async def _server(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def test_ami_and_nut_use_the_shared_bounded_tcp_transport():
    assert issubclass(AsteriskClient, BaseTcpTextClient)
    assert issubclass(NutUpsClient, BaseTcpTextClient)


async def test_tcp_transport_enforces_total_line_limit():
    async def handler(reader, writer):
        writer.write(b"one\ntwo\nthree\n")
        await writer.drain()
        writer.close()

    server, port = await _server(handler)
    try:
        client = ProbeClient(port)
        client.max_response_lines = 2
        with pytest.raises(ProviderError) as exc_info:
            await client.read_lines(3)
    finally:
        server.close()
        await server.wait_closed()

    assert exc_info.value.code == "invalid_response"
    assert "line limit" in exc_info.value.message


async def test_tcp_transport_rejects_oversized_line():
    async def handler(reader, writer):
        writer.write(b"x" * 128 + b"\n")
        await writer.drain()
        writer.close()

    server, port = await _server(handler)
    try:
        client = ProbeClient(port)
        client.max_line_bytes = 32
        with pytest.raises(ProviderError) as exc_info:
            await client.read_lines(1)
    finally:
        server.close()
        await server.wait_closed()

    assert exc_info.value.code == "invalid_response"
    assert "oversized line" in exc_info.value.message


async def test_tcp_transport_normalizes_operation_timeout():
    async def handler(reader, writer):
        await asyncio.sleep(0.2)
        writer.close()

    server, port = await _server(handler)
    try:
        client = ProbeClient(port)
        client.timeout_seconds = 0.05
        with pytest.raises(ProviderError) as exc_info:
            await client.read_lines(1)
    finally:
        server.close()
        await server.wait_closed()

    assert exc_info.value.code == "timeout"
