"""NUT UPS provider tests against an in-process fake upsd server."""

import asyncio

import pytest

from app.domain.actors import Actor
from app.providers.errors import ProviderError
from app.providers.nutups.client import NutUpsClient
from app.tools.execution import execute_tool

OPERATOR = Actor(kind="user", id="operator", label="operator")


class FakeNutServer:
    def __init__(self, *, require_auth: bool = False, on_battery: bool = False) -> None:
        self.require_auth = require_auth
        self.on_battery = on_battery
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        authed = not self.require_auth
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                command = line.decode().strip()
                if command.startswith("USERNAME "):
                    writer.write(b"OK\n")
                    await writer.drain()
                elif command.startswith("PASSWORD "):
                    authed = command == "PASSWORD secret"
                    writer.write(b"OK\n" if authed else b"ERR ACCESS-DENIED\n")
                    await writer.drain()
                elif command == "LIST UPS":
                    if not authed:
                        writer.write(b"ERR USERNAME-REQUIRED\n")
                    else:
                        writer.write(
                            b'BEGIN LIST UPS\nUPS ups "Rack UPS"\nEND LIST UPS\n'
                        )
                    await writer.drain()
                elif command == "LIST VAR ups":
                    if not authed:
                        writer.write(b"ERR USERNAME-REQUIRED\n")
                    else:
                        status = "OB LB" if self.on_battery else "OL"
                        writer.write(
                            (
                                "BEGIN LIST VAR ups\n"
                                f'VAR ups ups.status "{status}"\n'
                                'VAR ups ups.model "Smart-UPS 750"\n'
                                'VAR ups ups.mfr "APC"\n'
                                'VAR ups battery.charge "37"\n'
                                'VAR ups battery.runtime "540"\n'
                                'VAR ups ups.load "23"\n'
                                "END LIST VAR ups\n"
                            ).encode()
                        )
                    await writer.drain()
                else:
                    writer.write(b"ERR UNKNOWN-COMMAND\n")
                    await writer.drain()
        finally:
            writer.close()


def _configure(monkeypatch, port: int, *, auth: bool = False) -> None:
    monkeypatch.setattr(
        "app.providers.nutups.client.provider_config",
        lambda _pid: {"host": "127.0.0.1", "port": port, "ups_name": "ups", "timeout_seconds": 3},
    )
    monkeypatch.setattr(
        "app.providers.nutups.client.get_provider_secrets",
        lambda _pid: {"username": "console", "password": "secret"} if auth else {},
    )


@pytest.fixture
async def nut_server():
    server = FakeNutServer()
    await server.start()
    yield server
    await server.stop()


async def test_nutups_status_normalized(monkeypatch, nut_server):
    _configure(monkeypatch, nut_server.port)

    result = await execute_tool("nutups.status", {}, OPERATOR)

    assert result.ok is True
    assert result.result is not None
    assert result.result["ups"]["name"] == "ups"
    assert result.result["ups"]["status"] == "online"
    assert result.result["ups"]["battery_charge_percent"] == 37.0
    assert result.result["ups"]["load_percent"] == 23.0


async def test_nutups_summary_flags_on_battery(monkeypatch):
    server = FakeNutServer(on_battery=True)
    await server.start()
    try:
        _configure(monkeypatch, server.port)
        result = await execute_tool("nutups.summary", {}, OPERATOR)
    finally:
        await server.stop()

    assert result.ok is True
    assert result.result is not None
    summary = result.result["summary"]
    assert summary["provider_id"] == "nutups"
    assert summary["severity"] == "critical"
    assert summary["metrics"]["status"] == "low_battery"


async def test_nutups_auth(monkeypatch):
    server = FakeNutServer(require_auth=True)
    await server.start()
    try:
        _configure(monkeypatch, server.port, auth=True)
        devices = await NutUpsClient().list_ups()
    finally:
        await server.stop()

    assert devices == [{"name": "ups", "description": "Rack UPS"}]


async def test_nutups_missing_auth_is_redacted(monkeypatch):
    server = FakeNutServer(require_auth=True)
    await server.start()
    try:
        _configure(monkeypatch, server.port, auth=False)
        with pytest.raises(ProviderError) as exc_info:
            await NutUpsClient().list_ups()
    finally:
        await server.stop()

    assert exc_info.value.code == "credentials_missing"
    assert "secret" not in str(exc_info.value)


async def test_nutups_rejects_configured_field_line_injection(monkeypatch, nut_server):
    _configure(monkeypatch, nut_server.port, auth=True)
    monkeypatch.setattr(
        "app.providers.nutups.client.get_provider_secrets",
        lambda _pid: {"username": "console\nLIST UPS", "password": "secret"},
    )

    with pytest.raises(ProviderError) as exc_info:
        await NutUpsClient().list_ups()

    assert exc_info.value.code == "configuration_missing"
    assert "console" not in str(exc_info.value)
