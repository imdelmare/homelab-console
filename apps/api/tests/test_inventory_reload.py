from pathlib import Path

from app.core.settings import get_settings
from app.services import inventory


def _write(path: Path, label: str) -> None:
    path.write_text(
        "topology:\n"
        "  nodes:\n"
        "    - id: service.test\n"
        f"      label: {label}\n"
        "      kind: service\n"
        "      layer: services\n",
        encoding="utf-8",
    )


def test_inventory_reloads_when_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "homelab.yml"
    _write(path, "First")
    monkeypatch.setenv("HOMELAB_CONFIG_PATH", str(path))
    get_settings.cache_clear()
    inventory.clear_cache()

    assert inventory.list_topology_nodes()[0].label == "First"
    first_version = inventory.inventory_status()["version"]

    _write(path, "Second version")

    assert inventory.list_topology_nodes()[0].label == "Second version"
    assert inventory.inventory_status()["version"] != first_version


def test_inventory_keeps_last_valid_copy_on_invalid_edit(tmp_path, monkeypatch):
    path = tmp_path / "homelab.yml"
    _write(path, "Known good")
    monkeypatch.setenv("HOMELAB_CONFIG_PATH", str(path))
    get_settings.cache_clear()
    inventory.clear_cache()
    assert inventory.list_topology_nodes()[0].label == "Known good"

    path.write_text("topology:\n  nodes: [\n", encoding="utf-8")

    assert inventory.list_topology_nodes()[0].label == "Known good"
    status = inventory.inventory_status()
    assert status["status"] == "stale"
    assert "Error" in status["warning"]
