"""Asterisk AMI client tests against an in-process fake AMI server."""

import asyncio

import pytest

from app.providers.asterisk.client import AsteriskClient
from app.providers.errors import ProviderError


class FakeAmiServer:
    """Speaks just enough AMI to exercise the client."""

    def __init__(self, accept_login: bool = True, pjsip: bool = True) -> None:
        self.accept_login = accept_login
        self.pjsip = pjsip
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
        writer.write(b"Asterisk Call Manager/9.0.0\r\n")
        await writer.drain()
        try:
            while True:
                block = await self._read_block(reader)
                if block is None:
                    break
                action = block.get("Action", "")
                if action == "Login":
                    if self.accept_login:
                        self._write(writer, {"Response": "Success", "Message": "Authentication accepted"})
                    else:
                        self._write(writer, {"Response": "Error", "Message": "Authentication failed"})
                elif action == "CoreStatus":
                    self._write(writer, {
                        "Response": "Success", "CoreStartupDate": "2026-07-01",
                        "CoreStartupTime": "08:00:00", "CoreCurrentCalls": "2",
                    })
                elif action == "CoreSettings":
                    self._write(writer, {
                        "Response": "Success", "AsteriskVersion": "20.5.0",
                        "AMIversion": "9.0.0", "CoreMaxCalls": "10",
                    })
                elif action == "CoreShowChannels":
                    self._write(writer, {"Response": "Success", "EventList": "start"})
                    self._write(writer, {
                        "Event": "CoreShowChannel", "Channel": "PJSIP/100-0001",
                        "ChannelStateDesc": "Up", "CallerIDNum": "100",
                        "Application": "Dial", "Duration": "00:01:23",
                    })
                    self._write(writer, {"Event": "CoreShowChannelsComplete", "ListItems": "1"})
                elif action == "PJSIPShowEndpoints":
                    if self.pjsip:
                        self._write(writer, {"Response": "Success", "EventList": "start"})
                        self._write(writer, {
                            "Event": "EndpointList", "ObjectName": "100",
                            "DeviceState": "Not in use", "Contacts": "100/sip:100@10.0.0.9",
                        })
                        self._write(writer, {"Event": "EndpointListComplete", "ListItems": "1"})
                    else:
                        self._write(writer, {"Response": "Error", "Message": "Invalid/unknown command"})
                elif action == "SIPpeers":
                    self._write(writer, {"Response": "Success", "EventList": "start"})
                    self._write(writer, {
                        "Event": "PeerEntry", "ObjectName": "100",
                        "Status": "OK (12 ms)", "IPaddress": "10.0.0.9", "Dynamic": "yes",
                    })
                    self._write(writer, {"Event": "PeerlistComplete", "ListItems": "1"})
                elif action == "Logoff":
                    self._write(writer, {"Response": "Goodbye"})
                    break
        finally:
            writer.close()

    @staticmethod
    async def _read_block(reader: asyncio.StreamReader) -> dict | None:
        block: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if not line:
                return block or None
            decoded = line.decode().rstrip("\r\n")
            if not decoded:
                if block:
                    return block
                continue
            key, sep, value = decoded.partition(":")
            if sep:
                block[key.strip()] = value.strip()

    @staticmethod
    def _write(writer: asyncio.StreamWriter, fields: dict[str, str]) -> None:
        writer.write(("".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n").encode())


def _configure(monkeypatch, port: int) -> None:
    monkeypatch.setattr(
        "app.providers.asterisk.client.get_provider_secrets",
        lambda _pid: {"host": "127.0.0.1", "port": port, "username": "console", "secret": "ami-pw"},
        raising=True,
    )


@pytest.fixture
async def ami_server():
    server = FakeAmiServer()
    await server.start()
    yield server
    await server.stop()


async def test_core_status_normalized(monkeypatch, ami_server):
    _configure(monkeypatch, ami_server.port)
    from app.providers.asterisk.tools import core_status

    core = (await core_status())["core"]
    assert core["version"] == "20.5.0"
    assert core["current_calls"] == "2"
    assert core["max_calls"] == "10"


async def test_channels_list(monkeypatch, ami_server):
    _configure(monkeypatch, ami_server.port)
    from app.providers.asterisk.tools import channels_list

    result = await channels_list()
    assert result["total"] == 1
    assert result["channels"][0]["channel"] == "PJSIP/100-0001"
    assert result["channels"][0]["state"] == "Up"


async def test_peers_pjsip(monkeypatch, ami_server):
    _configure(monkeypatch, ami_server.port)
    from app.providers.asterisk.tools import peers_list

    result = await peers_list()
    assert result["stack"] == "pjsip"
    assert result["peers"][0]["endpoint"] == "100"


async def test_peers_fallback_to_chan_sip(monkeypatch):
    server = FakeAmiServer(pjsip=False)
    await server.start()
    try:
        _configure(monkeypatch, server.port)
        from app.providers.asterisk.tools import peers_list

        result = await peers_list()
        assert result["stack"] == "chan_sip"
        assert result["peers"][0]["address"] == "10.0.0.9"
    finally:
        await server.stop()


async def test_auth_failure(monkeypatch):
    server = FakeAmiServer(accept_login=False)
    await server.start()
    try:
        _configure(monkeypatch, server.port)
        with pytest.raises(ProviderError) as exc_info:
            await AsteriskClient().run_action("CoreStatus")
        assert exc_info.value.code == "auth_failed"
        assert "ami-pw" not in str(exc_info.value)
    finally:
        await server.stop()


async def test_unreachable(monkeypatch, ami_server):
    port = ami_server.port
    await ami_server.stop()
    _configure(monkeypatch, port)
    with pytest.raises(ProviderError) as exc_info:
        await AsteriskClient().run_action("CoreStatus")
    assert exc_info.value.code in ("unreachable", "timeout")


async def test_action_whitelist(monkeypatch, ami_server):
    _configure(monkeypatch, ami_server.port)
    with pytest.raises(ProviderError) as exc_info:
        await AsteriskClient().run_action("Command")  # arbitrary CLI execution
    assert exc_info.value.code == "permission_denied"


async def test_ami_rejects_configured_field_line_injection(monkeypatch, ami_server):
    monkeypatch.setattr(
        "app.providers.asterisk.client.get_provider_secrets",
        lambda _pid: {
            "host": "127.0.0.1",
            "port": ami_server.port,
            "username": "console",
            "secret": "password\r\nAction: Command",
        },
    )

    with pytest.raises(ProviderError) as exc_info:
        await AsteriskClient().run_action("CoreStatus")

    assert exc_info.value.code == "configuration_missing"
    assert "password" not in str(exc_info.value)


async def test_health_healthy(monkeypatch, ami_server):
    _configure(monkeypatch, ami_server.port)
    from app.providers.asterisk.tools import AsteriskProvider

    health = await AsteriskProvider().health()
    assert health.status == "healthy"
