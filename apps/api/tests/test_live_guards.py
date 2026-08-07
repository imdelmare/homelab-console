import pytest

from app.core.settings import get_settings
from app.main import _enforce_live_guards


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "live")
    monkeypatch.setenv("SESSION_SECRET", "a" * 40)
    monkeypatch.setenv("AUTH_NOTIFICATION_ADAPTER", "telegram")
    # Keep the override empty rather than unsetting it, which would fall
    # through to a potentially non-empty local .env value.
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()


def test_missing_homelab_config_file_fails_fast_in_live(monkeypatch, live_env):
    monkeypatch.setenv("HOMELAB_CONFIG_PATH", "config/does-not-exist.yml")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="HOMELAB_CONFIG_PATH"):
        _enforce_live_guards()


def test_existing_homelab_config_file_passes_in_live(monkeypatch, live_env):
    monkeypatch.setenv("HOMELAB_CONFIG_PATH", "config/homelab.example.yml")
    get_settings.cache_clear()
    _enforce_live_guards()


def test_live_guards_are_skipped_in_test_mode():
    assert get_settings().app_env == "test"
    _enforce_live_guards()
