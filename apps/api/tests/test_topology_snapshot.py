import asyncio

from app.domain.actors import Actor
from app.services import topology_snapshot


ACTOR = Actor(kind="user", id="operator", label="Operator")


async def test_snapshot_cache_deduplicates_and_force_refreshes(monkeypatch):
    calls = 0

    async def fake_build(_actor):
        nonlocal calls
        calls += 1
        return {"generated_at": str(calls)}

    monkeypatch.setattr(topology_snapshot, "_build_snapshot", fake_build)
    topology_snapshot.clear_topology_snapshot_cache()

    first = await topology_snapshot.get_topology_snapshot(ACTOR)
    second = await topology_snapshot.get_topology_snapshot(ACTOR)
    forced = await topology_snapshot.get_topology_snapshot(ACTOR, force=True)

    assert first == second
    assert forced != first
    assert calls == 2


async def test_concurrent_snapshot_requests_share_one_refresh(monkeypatch):
    calls = 0

    async def fake_build(_actor):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"generated_at": str(calls)}

    monkeypatch.setattr(topology_snapshot, "_build_snapshot", fake_build)
    topology_snapshot.clear_topology_snapshot_cache()

    snapshots = await asyncio.gather(
        topology_snapshot.get_topology_snapshot(ACTOR),
        topology_snapshot.get_topology_snapshot(ACTOR),
        topology_snapshot.get_topology_snapshot(ACTOR),
    )

    assert snapshots == [snapshots[0], snapshots[0], snapshots[0]]
    assert calls == 1


async def test_failed_source_uses_last_good_value():
    topology_snapshot.clear_topology_snapshot_cache()

    async def healthy():
        return [{"id": "service.test"}]

    value, meta = await topology_snapshot._source("observations", healthy)
    assert meta["status"] == "fresh"

    async def failed():
        raise RuntimeError("temporarily unavailable")

    fallback, stale_meta = await topology_snapshot._source("observations", failed)
    assert fallback == value
    assert stale_meta["status"] == "stale"
    assert stale_meta["observed_at"] == meta["observed_at"]
