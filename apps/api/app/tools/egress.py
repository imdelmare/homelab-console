"""Narrow public-egress observation through a fixed geolocation endpoint."""

import httpx

from app.providers.errors import ProviderError

_META_URL = "https://ipwho.is/"


async def status() -> dict:
    try:
        async with httpx.AsyncClient(
            timeout=8.0,
            verify=True,
            headers={
                "Accept": "application/json",
                "User-Agent": "Homelab-Console-Egress-Observer/1.0",
            },
        ) as client:
            response = await client.get(_META_URL)
    except httpx.TimeoutException:
        raise ProviderError("timeout", "egress observation timed out") from None
    except httpx.HTTPError:
        raise ProviderError("unreachable", "egress observation is unreachable") from None
    if response.status_code != 200:
        raise ProviderError(
            "degraded", f"egress observation returned HTTP {response.status_code}"
        )
    try:
        raw = response.json()
    except ValueError:
        raise ProviderError(
            "invalid_response", "egress observation returned invalid JSON"
        ) from None
    if not isinstance(raw, dict):
        raise ProviderError(
            "invalid_response", "egress observation returned an invalid shape"
        )
    if raw.get("success") is False:
        raise ProviderError("degraded", "egress observation rejected the request")
    ip = str(raw.get("ip") or "").strip()
    country = str(raw.get("country_code") or "").strip().upper()
    if not ip or len(country) != 2:
        raise ProviderError(
            "invalid_response", "egress observation omitted IP or country"
        )
    return {
        "public_ip": ip,
        "country_code": country,
        "city": str(raw.get("city") or "").strip(),
        "network": str((raw.get("connection") or {}).get("isp") or "").strip()
        if isinstance(raw.get("connection"), dict)
        else "",
        "provider": "ipwhois",
    }
