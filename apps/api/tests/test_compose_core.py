from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = ROOT / "deploy" / "compose" / "compose.yaml"
GHCR_COMPOSE_PATH = ROOT / "deploy" / "compose" / "compose.ghcr.yaml"
LEGACY_COMPOSE_PATH = ROOT / "deploy" / "docker-compose.yml"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _ghcr_compose() -> dict:
    return yaml.safe_load(GHCR_COMPOSE_PATH.read_text(encoding="utf-8"))


def test_core_compose_has_only_supported_services():
    compose = _compose()

    assert set(compose["services"]) == {"db", "api", "mcp-http", "web"}
    serialized = COMPOSE_PATH.read_text(encoding="utf-8").lower()
    for excluded in ("sentinel", "ollama", "ai_manager", "opencode_go_api_key"):
        assert excluded not in serialized
    assert "docker.sock" not in serialized


def test_core_compose_keeps_sensitive_services_private_and_models_disabled():
    services = _compose()["services"]

    assert "ports" not in services["db"]
    assert "ports" not in services["api"]
    assert services["web"]["ports"] == [
        "${WEB_BIND_ADDRESS:-127.0.0.1}:${WEB_PORT:-8080}:80"
    ]
    assert services["mcp-http"]["ports"] == [
        "${MCP_BIND_ADDRESS:-127.0.0.1}:${MCP_PORT:-8765}:8765"
    ]
    api_environment = services["api"]["environment"]
    assert api_environment["CONVERSATION_ENABLED"] == "false"
    assert api_environment["TASK_ROUTER_ENABLED"] == "false"
    assert api_environment["TELEGRAM_MEDIA_ENABLED"] == "false"
    assert "FIXER_DISPATCH_ENABLED" not in api_environment


def test_core_compose_mounts_operator_config_read_only():
    services = _compose()["services"]

    api_mounts = services["api"]["volumes"]
    mcp_mounts = services["mcp-http"]["volumes"]
    assert all(mount.endswith(":ro") for mount in api_mounts if "/app/config/" in mount)
    assert all(
        mount.endswith(":ro") for mount in mcp_mounts if "/app/apps/api/config/" in mount
    )
    assert not any(mount.startswith("../../config:") for mount in api_mounts + mcp_mounts)


def test_ghcr_compose_uses_only_public_core_images():
    compose = _ghcr_compose()
    services = compose["services"]

    assert set(services) == {"db", "api", "mcp-http", "web"}
    assert services["db"]["image"] == "postgres:16-alpine"
    assert services["api"]["image"].endswith("homelab-mcp-api:main}")
    assert services["mcp-http"]["image"].endswith("homelab-mcp-mcp:main}")
    assert services["web"]["image"].endswith("homelab-mcp-web:main}")
    assert all("build" not in service for service in services.values())

    serialized = GHCR_COMPOSE_PATH.read_text(encoding="utf-8").lower()
    for excluded in ("sentinel", "ollama", "ai_manager", "opencode_go_api_key"):
        assert excluded not in serialized
    assert "docker.sock" not in serialized


def test_ghcr_compose_preserves_private_bindings_and_read_only_config():
    services = _ghcr_compose()["services"]

    assert "ports" not in services["db"]
    assert "ports" not in services["api"]
    assert services["web"]["ports"] == [
        "${WEB_BIND_ADDRESS:-127.0.0.1}:${WEB_PORT:-8080}:80"
    ]
    assert services["mcp-http"]["ports"] == [
        "${MCP_BIND_ADDRESS:-127.0.0.1}:${MCP_PORT:-8765}:8765"
    ]
    for service_name in ("api", "mcp-http"):
        config_mounts = [
            mount for mount in services[service_name]["volumes"] if "/config/" in mount
        ]
        assert config_mounts
        assert all(mount.endswith(":ro") for mount in config_mounts)


def test_legacy_compose_passes_optional_sentinel_heartbeat_settings():
    if not LEGACY_COMPOSE_PATH.exists():
        pytest.skip("legacy native/fallback Compose is outside the Community export")
    services = yaml.safe_load(LEGACY_COMPOSE_PATH.read_text(encoding="utf-8"))["services"]
    environment = services["api"]["environment"]

    assert environment["SENTINEL_HEARTBEAT_ENABLED"] == "${SENTINEL_HEARTBEAT_ENABLED:-false}"
    assert environment["SENTINEL_HEARTBEAT_TOKEN"] == "${SENTINEL_HEARTBEAT_TOKEN:-}"
