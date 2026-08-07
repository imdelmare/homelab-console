import pytest

from app.core.settings import get_settings
from app.providers.errors import ProviderError
from app.providers.tls_policy import enforce_tls_policy


def _set_live(monkeypatch, *, allow: bool) -> None:
    monkeypatch.setenv("APP_ENV", "live")
    monkeypatch.setenv("ALLOW_INSECURE_LOCAL_TLS", "true" if allow else "false")
    get_settings.cache_clear()


def test_live_local_tls_opt_out_requires_explicit_flag(monkeypatch):
    _set_live(monkeypatch, allow=False)

    with pytest.raises(ProviderError) as exc_info:
        enforce_tls_policy(
            provider_id="proxmox",
            base_url="https://10.0.0.2:8006",
            verify_tls=False,
        )

    assert exc_info.value.code == "configuration_missing"


def test_live_local_tls_opt_out_accepts_private_ip(monkeypatch):
    _set_live(monkeypatch, allow=True)

    enforce_tls_policy(
        provider_id="proxmox",
        base_url="https://10.0.0.2:8006",
        verify_tls=False,
    )


def test_live_local_tls_opt_out_rejects_public_endpoint(monkeypatch):
    _set_live(monkeypatch, allow=True)

    with pytest.raises(ProviderError) as exc_info:
        enforce_tls_policy(
            provider_id="external",
            base_url="https://8.8.8.8",
            verify_tls=False,
        )

    assert exc_info.value.code == "configuration_missing"
    assert "private IP" in exc_info.value.message
