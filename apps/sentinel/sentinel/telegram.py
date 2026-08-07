from __future__ import annotations

import json
import urllib.request

from .config import TelegramConfig


class TelegramNotifier:
    def __init__(self, config: TelegramConfig) -> None:
        self.config = config

    @property
    def enabled(self) -> bool:
        return bool(self.config.bot_token and self.config.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
        body = json.dumps(
            {
                "chat_id": self.config.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_seconds):
            return True


def alert_text(title: str, description: str, occurrences: int) -> str:
    signal_label = "signal" if occurrences == 1 else "signals"
    verb = "requires" if occurrences == 1 else "require"
    return (
        "🚨 SENTINEL · ALERT\n\n"
        f"{title}\n"
        f"{occurrences} {signal_label} {verb} attention.\n\n"
        f"{description}\n\n"
        "Status: active"
    )


def recovery_text(title: str) -> str:
    return (
        "✅ SENTINEL · RECOVERED\n\n"
        f"{title}\n"
        "All correlated signals have returned to normal."
    )
