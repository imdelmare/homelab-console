"""Focused tests for the fixed-endpoint public-egress observation."""

import httpx
import pytest

from app.providers.errors import ProviderError
from app.tools import egress


def _mock_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("verify", None)
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(egress.httpx, "AsyncClient", client_factory)


async def test_egress_status_uses_only_fixed_endpoint_and_normalizes(monkeypatch):
    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == "https://ipwho.is/"
        return httpx.Response(
            200,
            json={
                "success": True,
                "ip": "203.0.113.8",
                "country_code": "de",
                "city": "Falkenstein",
                "connection": {"isp": "Example Network"},
            },
        )

    _mock_http(monkeypatch, handler)
    assert await egress.status() == {
        "public_ip": "203.0.113.8",
        "country_code": "DE",
        "city": "Falkenstein",
        "network": "Example Network",
        "provider": "ipwhois",
    }


async def test_egress_status_normalizes_provider_rejection(monkeypatch):
    _mock_http(
        monkeypatch,
        lambda _request: httpx.Response(200, json={"success": False}),
    )

    with pytest.raises(ProviderError) as exc_info:
        await egress.status()
    assert exc_info.value.code == "degraded"


async def test_egress_status_rejects_incomplete_response(monkeypatch):
    _mock_http(
        monkeypatch,
        lambda _request: httpx.Response(
            200, json={"success": True, "ip": "203.0.113.8"}
        ),
    )

    with pytest.raises(ProviderError) as exc_info:
        await egress.status()
    assert exc_info.value.code == "invalid_response"


async def test_egress_status_normalizes_timeout(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    _mock_http(monkeypatch, handler)
    with pytest.raises(ProviderError) as exc_info:
        await egress.status()
    assert exc_info.value.code == "timeout"
