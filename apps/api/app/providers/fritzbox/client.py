"""Async FritzBox TR-064 SOAP client.

The integration intentionally exposes only narrow read-only actions. FritzBox
TR-064 commonly uses HTTP Digest authentication on port 49000.
"""

from __future__ import annotations

import hashlib
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import httpx

from app.providers.errors import ProviderError
from app.providers.tls_policy import enforce_tls_policy
from app.services.inventory import get_host, provider_config
from app.services.secrets import get_provider_secrets, load_credentials_env, load_secrets


_FALLBACK_SERVICE_PATHS = {
    "DeviceInfo": "/upnp/control/deviceinfo",
    "WANCommonInterfaceConfig": "/upnp/control/wancommonifconfig1",
    "WLANConfiguration": "/upnp/control/wlanconfig{index}",
}

_FALLBACK_SERVICE_TYPES = {
    "DeviceInfo": "urn:dslforum-org:service:DeviceInfo:1",
    "WANCommonInterfaceConfig": "urn:dslforum-org:service:WANCommonInterfaceConfig:1",
    "WLANConfiguration": "urn:dslforum-org:service:WLANConfiguration:1",
}


@dataclass(frozen=True)
class FritzBoxTarget:
    provider_id: str
    host_id: str
    display_name: str
    env_prefix: str


TARGETS = {
    "fritzbox_primary": FritzBoxTarget(
        provider_id="fritzbox_primary",
        host_id="fritzbox-primary",
        display_name="FritzBox Primary",
        env_prefix="FRITZBOX_PRIMARY",
    ),
    "fritzbox_secondary": FritzBoxTarget(
        provider_id="fritzbox_secondary",
        host_id="fritzbox-secondary",
        display_name="FritzBox Secondary",
        env_prefix="FRITZBOX_SECONDARY",
    ),
}


def _legacy_key(provider_id: str) -> str:
    return provider_id


def _with_default_scheme_and_port(value: str) -> str:
    endpoint = value.strip().rstrip("/")
    if not endpoint:
        return ""
    if "://" not in endpoint:
        endpoint = f"http://{endpoint}"
    parsed = httpx.URL(endpoint)
    if parsed.port is None:
        parsed = parsed.copy_with(port=49000)
    return str(parsed).rstrip("/")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _response_key(name: str) -> str:
    key = name[3:] if name.startswith("New") else name
    if key.startswith("WAN"):
        return f"wan{key[3:]}"
    if key.startswith("WLAN"):
        return f"wlan{key[4:]}"
    if key == "SSID":
        return "ssid"
    return key[:1].lower() + key[1:]


def _parse_values(xml_text: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ProviderError("invalid_response", "fritzbox returned invalid XML") from exc

    values: dict[str, Any] = {}
    for node in root.iter():
        name = _local_name(node.tag)
        if name.startswith("New") and node.text is not None:
            values[_response_key(name)] = node.text
    return values


def _parse_services(xml_text: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ProviderError("invalid_response", "fritzbox returned invalid XML") from exc

    services = []
    for service in root.iter():
        if _local_name(service.tag) != "service":
            continue
        values = {_local_name(child.tag): child.text or "" for child in service}
        service_type = values.get("serviceType", "")
        control_url = values.get("controlURL", "")
        if service_type and control_url:
            services.append({"service_type": service_type, "control_url": control_url})
    return services


class FritzBoxClient:
    def __init__(self, provider_id: str) -> None:
        if provider_id not in TARGETS:
            raise ValueError(f"unknown fritzbox provider: {provider_id}")
        self.target = TARGETS[provider_id]
        self.provider_id = provider_id

        config = provider_config(provider_id)
        host = get_host(self.target.host_id)
        secrets = get_provider_secrets(provider_id)
        all_secrets = load_secrets()
        legacy_secrets = (all_secrets.get("legacy") or {}).get(_legacy_key(provider_id), {})
        env = load_credentials_env()

        legacy_endpoint = ((config.get("legacy_endpoints") or {}).get(provider_id) or {})
        raw_base_url = (
            config.get("base_url")
            or secrets.get("base_url")
            or legacy_endpoint.get("base_url")
            or legacy_endpoint.get("host")
            or (host.address if host else "")
        )
        self.base_url = _with_default_scheme_and_port(str(raw_base_url or ""))
        self.username = str(
            secrets.get("username")
            or legacy_secrets.get("username")
            or env.get(f"{self.target.env_prefix}_USER")
            or env.get(f"{self.target.env_prefix}_USERNAME")
            or ""
        )
        self.password = str(
            secrets.get("password")
            or legacy_secrets.get("password")
            or env.get(f"{self.target.env_prefix}_PASSWORD")
            or ""
        )
        self.verify_tls = bool(config.get("verify_tls", secrets.get("verify_tls", True)))
        self.timeout_seconds = float(config.get("timeout_seconds", secrets.get("timeout_seconds", 6.0)))
        self._services: list[dict[str, str]] | None = None

    def is_configured(self) -> bool:
        return bool(self.base_url)

    def has_credentials(self) -> bool:
        return bool(self.username and self.password)

    def _check_tls_policy(self) -> None:
        enforce_tls_policy(
            provider_id=self.provider_id,
            base_url=self.base_url,
            verify_tls=self.verify_tls,
        )

    async def get_description(self) -> str:
        return await self._request("GET", "/tr64desc.xml")

    async def _service(self, service: str, index: int | None) -> tuple[str, str]:
        if self._services is None:
            self._services = _parse_services(await self.get_description())
        prefix = f"urn:dslforum-org:service:{service}:"
        candidates = [item for item in self._services if item["service_type"].startswith(prefix)]
        if index is not None:
            selected = next((item for item in candidates if item["service_type"].endswith(f":{index}")), None)
        else:
            selected = candidates[0] if candidates else None
        if selected:
            return selected["service_type"], selected["control_url"]
        if service == "WANCommonInterfaceConfig":
            raise ProviderError("invalid_response", f"{self.provider_id} does not expose WANCommonInterfaceConfig")
        path_template = _FALLBACK_SERVICE_PATHS[service]
        path = path_template.format(index=index) if index is not None else path_template
        return _FALLBACK_SERVICE_TYPES[service], path

    async def call(
        self,
        service: str,
        action: str,
        *,
        index: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        service_type, path = await self._service(service, index)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f"<s:Body><u:{action} xmlns:u=\"{service_type}\" /></s:Body>"
            "</s:Envelope>"
        )
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SoapAction": f'"{service_type}#{action}"',
        }
        xml_text = await self._request("POST", path, content=body, headers=headers, timeout=timeout)
        return _parse_values(xml_text)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        content: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        if not self.is_configured():
            raise ProviderError("configuration_missing", f"{self.provider_id} base_url is not configured")
        self._check_tls_policy()
        auth = httpx.DigestAuth(self.username, self.password) if self.has_credentials() else None
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(
                verify=self.verify_tls,
                timeout=timeout or self.timeout_seconds,
                auth=auth,
                headers=headers,
            ) as client:
                response = await client.request(method, url, content=content)
        except httpx.TimeoutException:
            raise ProviderError("timeout", f"{self.provider_id} did not respond within the timeout ({path})")
        except httpx.ConnectError as exc:
            if isinstance(exc.__cause__, ssl.SSLError) or "ssl" in str(exc).lower():
                raise ProviderError("tls_error", f"TLS handshake with {self.provider_id} failed")
            raise ProviderError("unreachable", f"{self.provider_id} host is unreachable")
        except httpx.HTTPError as exc:
            raise ProviderError("unreachable", f"{self.provider_id} request failed: {exc.__class__.__name__}")

        if response.status_code == 401:
            raise ProviderError("auth_failed", f"{self.provider_id} rejected the credentials")
        if response.status_code == 403:
            raise ProviderError("permission_denied", f"{self.provider_id} credentials lack permission")
        if response.status_code >= 500:
            raise ProviderError("degraded", f"{self.provider_id} returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ProviderError("invalid_response", f"{self.provider_id} returned HTTP {response.status_code}")
        return response.text


# --- Web UI session (login_sid.lua) -----------------------------------------
#
# TR-064 has no action for the box's own temperature; the only source is the
# undocumented web UI endpoint data.lua page=ecoStat, which needs a web
# session (challenge-response login), not the TR-064 digest credentials.

_EMPTY_SID = "0000000000000000"


def _challenge_response(challenge: str, password: str) -> str:
    if challenge.startswith("2$"):
        _, iterations1, salt1, iterations2, salt2 = challenge.split("$")
        hash1 = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt1), int(iterations1))
        hash2 = hashlib.pbkdf2_hmac("sha256", hash1, bytes.fromhex(salt2), int(iterations2))
        return f"{salt2}${hash2.hex()}"
    digest = hashlib.md5(f"{challenge}-{password}".encode("utf-16-le")).hexdigest()
    return f"{challenge}-{digest}"


class FritzBoxWebSession:
    """Minimal read-only session against the FritzBox web UI on port 80."""

    def __init__(self, client: "FritzBoxClient") -> None:
        if not client.is_configured():
            raise ProviderError("configuration_missing", f"{client.provider_id} base_url is not configured")
        if not client.has_credentials():
            raise ProviderError("credentials_missing", f"{client.provider_id} username/password is not configured")
        host = httpx.URL(client.base_url).host
        self.provider_id = client.provider_id
        self.base = f"http://{host}"
        self.username = client.username
        self.password = client.password
        self.timeout_seconds = client.timeout_seconds

    async def get_ecostat(self) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as web:
                sid = await self._login(web)
                try:
                    response = await web.post(
                        f"{self.base}/data.lua",
                        data={"xhr": "1", "sid": sid, "page": "ecoStat", "lang": "en"},
                    )
                finally:
                    await web.get(f"{self.base}/login_sid.lua", params={"logout": "1", "sid": sid})
        except httpx.TimeoutException:
            raise ProviderError("timeout", f"{self.provider_id} web UI did not respond within the timeout")
        except httpx.HTTPError as exc:
            raise ProviderError("unreachable", f"{self.provider_id} web UI request failed: {exc.__class__.__name__}")
        if response.status_code >= 400:
            raise ProviderError("invalid_response", f"{self.provider_id} web UI returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ProviderError("invalid_response", f"{self.provider_id} web UI returned a non-JSON ecoStat payload")

    async def _login(self, web: httpx.AsyncClient) -> str:
        response = await web.get(f"{self.base}/login_sid.lua", params={"version": "2"})
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise ProviderError("invalid_response", f"{self.provider_id} web UI returned invalid login XML") from exc
        sid = root.findtext("SID") or ""
        if sid and sid != _EMPTY_SID:
            return sid
        challenge = root.findtext("Challenge") or ""
        listed_users = [item.text or "" for item in root.iter("User")]
        username = self.username or (listed_users[0] if listed_users else "")
        response = await web.post(
            f"{self.base}/login_sid.lua?version=2",
            data={"username": username, "response": _challenge_response(challenge, self.password)},
        )
        try:
            sid = ET.fromstring(response.text).findtext("SID") or ""
        except ET.ParseError as exc:
            raise ProviderError("invalid_response", f"{self.provider_id} web UI returned invalid login XML") from exc
        if not sid or sid == _EMPTY_SID:
            raise ProviderError("auth_failed", f"{self.provider_id} web UI rejected the credentials")
        return sid
