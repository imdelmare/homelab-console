from __future__ import annotations

import json
import os
import platform
import socket
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class HeartbeatClientConfig:
    url: str
    token: str
    timeout_seconds: float = 8.0
    source: str = "homelab-console"


def load_client_config(environ: dict[str, str] | None = None) -> HeartbeatClientConfig:
    env = environ or os.environ
    url = env.get("SENTINEL_HEARTBEAT_URL", "").strip()
    token = env.get("SENTINEL_HEARTBEAT_TOKEN", "").strip()
    if not url:
        raise ValueError("SENTINEL_HEARTBEAT_URL is required")
    if not token:
        raise ValueError("SENTINEL_HEARTBEAT_TOKEN is required")
    return HeartbeatClientConfig(
        url=url,
        token=token,
        timeout_seconds=float(env.get("SENTINEL_HEARTBEAT_TIMEOUT_SECONDS", "8")),
        source=env.get("SENTINEL_HEARTBEAT_SOURCE", "homelab-console"),
    )


def build_payload(config: HeartbeatClientConfig) -> dict[str, Any]:
    return {
        "source": config.source,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "platform": platform.platform(),
    }


def send_heartbeat(config: HeartbeatClientConfig, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload or build_payload(config), sort_keys=True).encode("utf-8")
    request = urllib.request.Request(
        config.url,
        data=body,
        headers={
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
            "User-Agent": "homelab-sentinel-heartbeat/1",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
        raw = response.read(4096)
        if not raw:
            return {"ok": response.status == 200}
        decoded = json.loads(raw.decode("utf-8"))
        return decoded if isinstance(decoded, dict) else {"ok": False}


def main() -> None:
    try:
        result = send_heartbeat(load_client_config())
    except Exception as exc:  # noqa: BLE001 - CLI should fail with a concise operator error.
        print(f"heartbeat failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not result.get("ok"):
        print(f"heartbeat rejected: {result}", file=sys.stderr)
        raise SystemExit(1)
    print("heartbeat sent")


if __name__ == "__main__":
    main()
