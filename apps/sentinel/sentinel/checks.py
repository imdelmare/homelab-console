from __future__ import annotations

import urllib.error
import urllib.request
from datetime import timezone

from .config import HeartbeatConfig, TargetConfig
from .store import Observation, SentinelStore, utcnow


def check_http_target(target: TargetConfig) -> Observation:
    request = urllib.request.Request(target.url, headers={"User-Agent": "homelab-sentinel/1"})
    try:
        with urllib.request.urlopen(request, timeout=target.timeout_seconds) as response:
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
    except Exception as exc:  # noqa: BLE001 - error text is the operator-facing evidence.
        return Observation(
            dedupe_key=f"http:{target.id}",
            source_id=target.id,
            kind="http",
            ok=False,
            title=f"{target.name} health check failed",
            description=f"{type(exc).__name__}: {exc}",
            failure_confirmations=target.failure_confirmations,
            notification_group=target.notification_group,
        )

    ok = target.expected_status_min <= status <= target.expected_status_max
    return Observation(
        dedupe_key=f"http:{target.id}",
        source_id=target.id,
        kind="http",
        ok=ok,
        title=f"{target.name} health check failed",
        description=(
            f"HTTP {status} outside expected range "
            f"{target.expected_status_min}-{target.expected_status_max}"
            if not ok
            else f"HTTP {status} in expected range"
        ),
        failure_confirmations=target.failure_confirmations,
        notification_group=target.notification_group,
    )


def check_heartbeat(source: HeartbeatConfig, store: SentinelStore) -> Observation:
    last_seen = store.last_heartbeat_at(source.id)
    if last_seen is None:
        return Observation(
            dedupe_key=f"heartbeat:{source.id}",
            source_id=source.id,
            kind="heartbeat",
            ok=False,
            title=f"{source.name} heartbeat missing",
            description=f"No heartbeat received yet; timeout is {source.timeout_seconds}s",
            failure_confirmations=source.failure_confirmations,
            notification_group=source.notification_group,
        )
    age = (utcnow() - last_seen.astimezone(timezone.utc)).total_seconds()
    ok = age <= source.timeout_seconds
    return Observation(
        dedupe_key=f"heartbeat:{source.id}",
        source_id=source.id,
        kind="heartbeat",
        ok=ok,
        title=f"{source.name} heartbeat stale",
        description=(
            f"Last heartbeat age {age:.0f}s exceeds timeout {source.timeout_seconds}s"
            if not ok
            else f"Last heartbeat age {age:.0f}s"
        ),
        failure_confirmations=source.failure_confirmations,
        notification_group=source.notification_group,
    )
