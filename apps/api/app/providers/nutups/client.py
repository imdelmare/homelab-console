"""Minimal async Network UPS Tools (NUT) upsd client.

The client speaks only the read-oriented text protocol operations required by
this provider. There is no generic command passthrough.
"""

from __future__ import annotations

import shlex

from app.providers.errors import ProviderError, ProviderErrorCode
from app.providers.tcpclient import BaseTcpTextClient, TcpTextSession
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env


class NutUpsClient(BaseTcpTextClient):
    provider_id = "nutups"

    def __init__(self) -> None:
        super().__init__()
        config = provider_config(self.provider_id)
        secrets = get_provider_secrets(self.provider_id)
        env = load_credentials_env()

        self.host = str(config.get("host") or secrets.get("host") or env.get("NUT_UPS_HOST") or "")
        self.port = int(config.get("port") or secrets.get("port") or env.get("NUT_UPS_PORT") or 3493)
        self.ups_name = str(config.get("ups_name") or secrets.get("ups_name") or env.get("NUT_UPS_NAME") or "")
        self.username = str(secrets.get("username") or env.get("NUT_UPS_USERNAME") or "")
        self.password = str(secrets.get("password") or env.get("NUT_UPS_PASSWORD") or "")
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 5.0)))

    def has_credentials(self) -> bool:
        return bool(self.username and self.password)

    async def list_ups(self) -> list[dict[str, str]]:
        async def inner() -> list[dict[str, str]]:
            async with self.connection() as session:
                await self._authenticate(session)
                lines = await self._command_list(session, "LIST UPS", "UPS")
                devices: list[dict[str, str]] = []
                for line in lines:
                    parts = _split(line)
                    if len(parts) >= 2 and parts[0] == "UPS":
                        devices.append({"name": parts[1], "description": parts[2] if len(parts) > 2 else ""})
                return devices

        return await self.execute("list UPS devices", inner)

    async def variables(self, ups_name: str | None = None) -> dict[str, str]:
        name = ups_name or self.ups_name or await self.default_ups_name()

        async def inner() -> dict[str, str]:
            async with self.connection() as session:
                await self._authenticate(session)
                lines = await self._command_list(session, f"LIST VAR {name}", "VAR")
                variables: dict[str, str] = {}
                for line in lines:
                    parts = _split(line)
                    if len(parts) >= 4 and parts[0] == "VAR" and parts[1] == name:
                        variables[parts[2]] = parts[3]
                return variables

        return await self.execute(f"list variables for {name}", inner)

    async def default_ups_name(self) -> str:
        devices = await self.list_ups()
        if not devices:
            raise ProviderError("invalid_response", "nut upsd did not return any UPS device")
        return devices[0]["name"]

    async def _authenticate(self, session: TcpTextSession) -> None:
        if not self.has_credentials():
            return
        await self._command_line(session, f"USERNAME {self.username}")
        await self._command_line(session, f"PASSWORD {self.password}")

    async def _command_line(self, session: TcpTextSession, command: str) -> str:
        await session.write_line(command)
        line = await session.read_line()
        if line.startswith("ERR "):
            raise _provider_error(line)
        return line

    async def _command_list(
        self,
        session: TcpTextSession,
        command: str,
        item_prefix: str,
    ) -> list[str]:
        await session.write_line(command)
        begin = await session.read_line()
        if begin.startswith("ERR "):
            raise _provider_error(begin)
        if begin != f"BEGIN {command}":
            raise ProviderError("invalid_response", f"unexpected nut response to {command}")
        lines: list[str] = []
        while True:
            line = await session.read_line()
            if line == f"END {command}":
                return lines
            if line.startswith("ERR "):
                raise _provider_error(line)
            if line.startswith(f"{item_prefix} "):
                lines.append(line)


def _split(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError as exc:
        raise ProviderError("invalid_response", "nut upsd returned malformed quoting") from exc


def _provider_error(line: str) -> ProviderError:
    code = line.removeprefix("ERR ").strip().split(" ", 1)[0]
    mapping: dict[str, ProviderErrorCode] = {
        "ACCESS-DENIED": "auth_failed",
        "USERNAME-REQUIRED": "credentials_missing",
        "PASSWORD-REQUIRED": "credentials_missing",
        "UNKNOWN-UPS": "invalid_response",
        "UNKNOWN-COMMAND": "permission_denied",
    }
    mapped = mapping.get(code, "invalid_response")
    return ProviderError(mapped, f"nut upsd returned {code.lower().replace('-', '_')}")
