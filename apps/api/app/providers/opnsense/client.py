"""Async OPNsense API client (API key/secret basic auth).

Username/password login is intentionally not used for provider tools.
"""

from typing import Literal

from app.providers.httpclient import BaseJsonClient
from app.services.inventory import provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env


class OpnsenseClient(BaseJsonClient):
    provider_id = "opnsense"

    def __init__(self, credential_profile: Literal["reader", "wol"] = "reader") -> None:
        super().__init__()
        secrets = get_provider_secrets("opnsense")
        config = provider_config("opnsense")
        env = load_credentials_env()
        self.credential_profile = credential_profile

        host = env.get("OPNSENSE_HOST") or ""
        self.base_url = str(
            config.get("base_url")
            or secrets.get("base_url")
            or env.get("OPNSENSE_URL")
            or (f"https://{host}" if host else "")
        ).rstrip("/")
        if credential_profile == "wol":
            api_key = (
                secrets.get("wol_api_key") or env.get("OPNSENSE_WOL_API_KEY") or ""
            )
            api_secret = (
                secrets.get("wol_api_secret")
                or env.get("OPNSENSE_WOL_API_SECRET")
                or ""
            )
        else:
            api_key = secrets.get("api_key") or env.get("OPNSENSE_API_KEY") or ""
            api_secret = (
                secrets.get("api_secret") or env.get("OPNSENSE_API_SECRET") or ""
            )
        self.auth = (api_key, api_secret) if api_key and api_secret else None
        self.verify_tls = bool(config.get("verify_tls", secrets.get("verify_tls", True)))
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 6.0)))

    def has_credentials(self) -> bool:
        return self.auth is not None

    def credentials_error(self) -> str:
        if self.credential_profile == "wol":
            return (
                "opnsense Wake-on-LAN API key/secret is not configured "
                "(wol_api_key/wol_api_secret)"
            )
        return "opnsense API key/secret is not configured"
