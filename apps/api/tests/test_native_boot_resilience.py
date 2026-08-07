from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = ROOT / "deploy" / "systemd"


def _unit(name: str) -> str:
    return (SYSTEMD_DIR / name).read_text(encoding="utf-8")


def test_api_waits_for_postgres_without_requiring_docker() -> None:
    unit = _unit("homelab-console-api.service")

    assert "After=network-online.target" in unit
    assert "docker.service" not in unit
    assert "ExecStartPre=/opt/homelab-console/scripts/native-runtime.sh wait-db" in unit
    assert unit.index(" wait-db") < unit.index(" migrate")


def test_mcp_retries_database_readiness_without_requiring_api_or_docker() -> None:
    unit = _unit("homelab-console-mcp.service")

    assert "After=network-online.target" in unit
    assert "docker.service" not in unit
    assert "ExecStartPre=/opt/homelab-console/scripts/native-runtime.sh wait-db" in unit
    assert "Requires=homelab-console-api.service" not in unit
    assert "Restart=on-failure" in unit


def test_caddy_can_start_while_api_is_recovering() -> None:
    unit = _unit("homelab-console-caddy.service")

    assert "Wants=network-online.target homelab-console-api.service" in unit
    assert "Requires=homelab-console-api.service" not in unit
    assert "After=network-online.target homelab-console-api.service" not in unit
