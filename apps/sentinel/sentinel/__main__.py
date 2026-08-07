from __future__ import annotations

import sys

from .config import load_config
from .service import SentinelService


def main() -> None:
    config = load_config()
    if config.heartbeats and not config.heartbeat_token:
        print(
            "SENTINEL_HEARTBEAT_TOKEN is not set but heartbeats are configured: the "
            "/heartbeat/* endpoint would fail closed (401) for every real client. "
            "Set SENTINEL_HEARTBEAT_TOKEN before starting.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    SentinelService(config).serve_forever()


if __name__ == "__main__":
    main()
