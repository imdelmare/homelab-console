"""Minimal async Asterisk Manager Interface (AMI) client, read-only.

Speaks the AMI text protocol over TCP: login, send one action, collect the
response (single block or event list). Only explicitly whitelisted read
actions can be sent — there is no generic command passthrough.
"""

from typing import Any

from app.providers.errors import ProviderError
from app.providers.tcpclient import BaseTcpTextClient, TcpTextSession
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env

# The complete set of AMI actions this client is allowed to send.
ALLOWED_ACTIONS = {
    "Login",
    "Logoff",
    "CoreStatus",
    "CoreSettings",
    "CoreShowChannels",
    "PJSIPShowEndpoints",
    "SIPpeers",
}

_LIST_COMPLETE_SUFFIXES = ("Complete",)


class AsteriskClient(BaseTcpTextClient):
    provider_id = "asterisk"

    def __init__(self) -> None:
        super().__init__()
        secrets = get_provider_secrets("asterisk")
        config = provider_config("asterisk")
        env = load_credentials_env()

        self.host = str(config.get("host") or secrets.get("host") or env.get("ASTERISK_HOST") or "")
        self.port = int(config.get("port") or secrets.get("port") or env.get("ASTERISK_AMI_PORT") or 5038)
        self.username = str(secrets.get("username") or env.get("ASTERISK_AMI_USER") or "")
        self.secret = str(secrets.get("secret") or env.get("ASTERISK_AMI_PASSWORD") or "")
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 6.0)))

    def has_credentials(self) -> bool:
        return bool(self.username and self.secret)

    async def run_action(self, action: str, collect_events: bool = False) -> dict[str, Any]:
        """Login, run one action, logoff. Returns {response: block, events: [...]}."""
        if action not in ALLOWED_ACTIONS:
            raise ProviderError("permission_denied", f"AMI action not allowed: {action}")
        if not self.is_configured():
            raise ProviderError("configuration_missing", "asterisk host is not configured")
        if not self.has_credentials():
            raise ProviderError("credentials_missing", "asterisk AMI username/secret is not configured")
        return await self.execute(
            f"AMI {action}", lambda: self._run_action_inner(action, collect_events)
        )

    async def _run_action_inner(self, action: str, collect_events: bool) -> dict[str, Any]:
        async with self.connection() as session:
            banner = await session.read_line()
            if "Asterisk" not in banner:
                raise ProviderError("invalid_response", "unexpected AMI banner")

            await self._send(session, {"Action": "Login", "Username": self.username,
                                      "Secret": self.secret, "Events": "off"})
            login = await self._read_block(session)
            if login.get("Response") != "Success":
                raise ProviderError("auth_failed", "asterisk rejected the AMI credentials")

            await self._send(session, {"Action": action})
            response = await self._read_block(session)
            if response.get("Response") == "Error":
                raise ProviderError(
                    "invalid_response",
                    f"AMI action {action} failed: {response.get('Message', 'unknown error')}",
                )

            events: list[dict[str, str]] = []
            if collect_events:
                while True:
                    block = await self._read_block(session)
                    event_name = block.get("Event", "")
                    if event_name.endswith(_LIST_COMPLETE_SUFFIXES):
                        break
                    events.append(block)

            await self._send(session, {"Action": "Logoff"})
            return {"response": response, "events": events}

    @staticmethod
    async def _send(session: TcpTextSession, fields: dict[str, str]) -> None:
        if any("\r" in item or "\n" in item for pair in fields.items() for item in pair):
            raise ProviderError(
                "configuration_missing", "AMI fields must not contain line breaks"
            )
        payload = "".join(f"{key}: {value}\r\n" for key, value in fields.items()) + "\r\n"
        await session.write_text(payload)

    @staticmethod
    async def _read_block(session: TcpTextSession) -> dict[str, str]:
        """Read one AMI block (terminated by an empty line)."""
        block: dict[str, str] = {}
        while True:
            decoded = await session.read_line()
            if not decoded:
                if block:
                    return block
                continue
            key, sep, value = decoded.partition(":")
            if sep:
                block[key.strip()] = value.strip()
