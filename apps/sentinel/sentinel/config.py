from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TargetConfig:
    id: str
    name: str
    url: str
    timeout_seconds: float = 5.0
    expected_status_min: int = 200
    expected_status_max: int = 399
    failure_confirmations: int = 3
    notification_group: str = "homelab-console-availability"


@dataclass(frozen=True)
class HeartbeatConfig:
    id: str
    name: str
    timeout_seconds: int = 180
    failure_confirmations: int = 1
    notification_group: str = "homelab-console-availability"


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    timeout_seconds: float = 8.0


@dataclass(frozen=True)
class SentinelConfig:
    loop_interval_seconds: int = 30
    state_path: str = "data/sentinel.sqlite3"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8766
    heartbeat_token: str = ""
    recovery_confirmations: int = 2
    aggregation_window_seconds: int = 120
    notification_cooldown_seconds: int = 1800
    targets: list[TargetConfig] = field(default_factory=list)
    heartbeats: list[HeartbeatConfig] = field(default_factory=list)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


def load_config(path: str | None = None, environ: dict[str, str] | None = None) -> SentinelConfig:
    env = environ or os.environ
    config_path = path or env.get("SENTINEL_CONFIG_PATH", "config/sentinel.local.json")
    raw: dict[str, Any] = {}
    if Path(config_path).exists():
        raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    elif path or "SENTINEL_CONFIG_PATH" in env:
        raise FileNotFoundError(config_path)

    raw.setdefault("telegram", {})
    if env.get("SENTINEL_STATE_PATH"):
        raw["state_path"] = env["SENTINEL_STATE_PATH"]
    if env.get("SENTINEL_HEARTBEAT_TOKEN"):
        raw["heartbeat_token"] = env["SENTINEL_HEARTBEAT_TOKEN"]
    if env.get("SENTINEL_LISTEN_HOST"):
        raw["listen_host"] = env["SENTINEL_LISTEN_HOST"]
    if env.get("SENTINEL_LISTEN_PORT"):
        raw["listen_port"] = int(env["SENTINEL_LISTEN_PORT"])
    if env.get("TELEGRAM_BOT_TOKEN"):
        raw["telegram"]["bot_token"] = env["TELEGRAM_BOT_TOKEN"]
    if env.get("TELEGRAM_ALLOWED_CHAT_ID"):
        raw["telegram"]["chat_id"] = env["TELEGRAM_ALLOWED_CHAT_ID"]

    return SentinelConfig(
        loop_interval_seconds=int(raw.get("loop_interval_seconds", 30)),
        state_path=str(raw.get("state_path", "data/sentinel.sqlite3")),
        listen_host=str(raw.get("listen_host", "127.0.0.1")),
        listen_port=int(raw.get("listen_port", 8766)),
        heartbeat_token=str(raw.get("heartbeat_token", "")),
        recovery_confirmations=max(1, int(raw.get("recovery_confirmations", 2))),
        aggregation_window_seconds=max(0, int(raw.get("aggregation_window_seconds", 120))),
        notification_cooldown_seconds=max(0, int(raw.get("notification_cooldown_seconds", 1800))),
        targets=[
            TargetConfig(
                id=str(item["id"]),
                name=str(item.get("name") or item["id"]),
                url=str(item["url"]),
                timeout_seconds=float(item.get("timeout_seconds", 5.0)),
                expected_status_min=int(item.get("expected_status_min", 200)),
                expected_status_max=int(item.get("expected_status_max", 399)),
                failure_confirmations=max(1, int(item.get("failure_confirmations", 3))),
                notification_group=str(item.get("notification_group", "homelab-console-availability")),
            )
            for item in raw.get("targets", [])
        ],
        heartbeats=[
            HeartbeatConfig(
                id=str(item["id"]),
                name=str(item.get("name") or item["id"]),
                timeout_seconds=int(item.get("timeout_seconds", 180)),
                failure_confirmations=max(1, int(item.get("failure_confirmations", 1))),
                notification_group=str(item.get("notification_group", "homelab-console-availability")),
            )
            for item in raw.get("heartbeats", [])
        ],
        telegram=TelegramConfig(
            bot_token=str(raw["telegram"].get("bot_token", "")),
            chat_id=str(raw["telegram"].get("chat_id", "")),
            timeout_seconds=float(raw["telegram"].get("timeout_seconds", 8.0)),
        ),
    )
