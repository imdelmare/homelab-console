"""Static dependency graph over the homelab topology.

Loaded from the `dependencies:` block of the homelab config
(`app.services.inventory.list_dependencies`). Used to walk from a symptom
toward likely root causes (`upstream_of`) and to narrate blast radius
(`downstream_of`/`path_from_root`), replacing pure keyword matching in
`task_context.py` and `watchers.py`.
"""

from __future__ import annotations

from functools import lru_cache

from app.services import inventory


@lru_cache
def _adjacency() -> dict[str, list[str]]:
    """node_id -> depends_on list, deduped. Self-edges and references to
    node ids not declared in the graph are dropped here so a config typo
    can't crash the app or create a phantom dependency."""
    nodes = inventory.list_dependencies()
    known = {node.id for node in nodes}
    adjacency: dict[str, list[str]] = {}
    for node in nodes:
        deps: list[str] = []
        for dep in node.depends_on:
            if dep == node.id or dep not in known or dep in deps:
                continue
            deps.append(dep)
        adjacency[node.id] = deps
    return adjacency


@lru_cache
def _reverse_adjacency() -> dict[str, list[str]]:
    reverse: dict[str, list[str]] = {node_id: [] for node_id in _adjacency()}
    for node_id, deps in _adjacency().items():
        for dep in deps:
            reverse.setdefault(dep, []).append(node_id)
    return reverse


def clear_cache() -> None:
    _adjacency.cache_clear()
    _reverse_adjacency.cache_clear()


def _transitive(node_id: str, adjacency: dict[str, list[str]]) -> list[str]:
    """BFS transitive closure, nearest-first, excluding node_id itself. The
    visited set makes this safe against a misconfigured cycle: expansion
    just stops instead of looping forever."""
    visited = {node_id}
    order: list[str] = []
    queue = list(adjacency.get(node_id, []))
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        order.append(current)
        queue.extend(adjacency.get(current, []))
    return order


def upstream_of(node_id: str) -> list[str]:
    """Transitive closure of what node_id depends on, nearest-first.
    Returns [] for an unconfigured/unknown node_id."""
    return _transitive(node_id, _adjacency())


def downstream_of(node_id: str) -> list[str]:
    """Transitive closure of what depends on node_id, nearest-first."""
    return _transitive(node_id, _reverse_adjacency())


def path_from_root(root_id: str, target_id: str) -> list[str] | None:
    """Shortest root -> ... -> target chain (inclusive both ends), walking
    downstream from root. None if target isn't reachable that way."""
    if root_id == target_id:
        return [root_id]
    reverse = _reverse_adjacency()
    visited = {root_id}
    parent: dict[str, str] = {}
    queue = [root_id]
    while queue:
        current = queue.pop(0)
        for nxt in reverse.get(current, []):
            if nxt in visited:
                continue
            visited.add(nxt)
            parent[nxt] = current
            if nxt == target_id:
                path = [target_id]
                while path[-1] != root_id:
                    path.append(parent[path[-1]])
                path.reverse()
                return path
            queue.append(nxt)
    return None


def label_of(node_id: str) -> str:
    node = inventory.get_dependency(node_id)
    if node is not None and node.label:
        return node.label
    return node_id
