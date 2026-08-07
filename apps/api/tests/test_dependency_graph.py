from app.services import dependency_graph, inventory


def _configure(monkeypatch, nodes: list[dict]) -> None:
    entries = [inventory.DependencyEntry(**item) for item in nodes]
    monkeypatch.setattr(inventory, "list_dependencies", lambda: entries)
    dependency_graph.clear_cache()


def test_upstream_of_transitive_closure_is_nearest_first(monkeypatch):
    _configure(
        monkeypatch,
        [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["b"]},
            {"id": "d", "depends_on": ["c"]},
        ],
    )
    assert dependency_graph.upstream_of("d") == ["c", "b", "a"]
    assert dependency_graph.upstream_of("a") == []


def test_downstream_of_transitive_closure(monkeypatch):
    _configure(
        monkeypatch,
        [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["b"]},
            {"id": "d", "depends_on": ["c"]},
        ],
    )
    assert dependency_graph.downstream_of("a") == ["b", "c", "d"]
    assert dependency_graph.downstream_of("d") == []


def test_cycle_does_not_hang_or_raise(monkeypatch):
    _configure(
        monkeypatch,
        [
            {"id": "a", "depends_on": ["b"]},
            {"id": "b", "depends_on": ["a"]},
        ],
    )
    assert dependency_graph.upstream_of("a") == ["b"]
    assert dependency_graph.downstream_of("a") == ["b"]


def test_unknown_node_id_returns_empty_without_raising(monkeypatch):
    _configure(monkeypatch, [{"id": "a", "depends_on": []}])
    assert dependency_graph.upstream_of("nonexistent") == []
    assert dependency_graph.downstream_of("nonexistent") == []


def test_edge_to_unknown_node_is_dropped(monkeypatch):
    _configure(monkeypatch, [{"id": "a", "depends_on": ["ghost"]}])
    assert dependency_graph.upstream_of("a") == []


def test_self_edge_is_dropped(monkeypatch):
    _configure(monkeypatch, [{"id": "a", "depends_on": ["a"]}])
    assert dependency_graph.upstream_of("a") == []


def test_path_from_root_shortest_chain(monkeypatch):
    _configure(
        monkeypatch,
        [
            {"id": "root", "depends_on": []},
            {"id": "mid", "depends_on": ["root"]},
            {"id": "leaf", "depends_on": ["mid"]},
            {"id": "unrelated", "depends_on": []},
        ],
    )
    assert dependency_graph.path_from_root("root", "leaf") == ["root", "mid", "leaf"]
    assert dependency_graph.path_from_root("root", "root") == ["root"]
    assert dependency_graph.path_from_root("root", "unrelated") is None


def test_label_of_falls_back_to_id(monkeypatch):
    _configure(
        monkeypatch,
        [
            {"id": "a", "label": "Alpha node", "depends_on": []},
            {"id": "b", "depends_on": []},
        ],
    )
    assert dependency_graph.label_of("a") == "Alpha node"
    assert dependency_graph.label_of("b") == "b"
    assert dependency_graph.label_of("nonexistent") == "nonexistent"
