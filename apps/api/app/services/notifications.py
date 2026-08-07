"""Notification adapters used to deliver second-factor login challenges.

The test adapter refuses to run in live mode. The Telegram adapter never logs
the OTP or the approval nonce.
"""

import logging
from typing import Protocol

import httpx

from app.core.settings import get_settings

logger = logging.getLogger("homelab.auth")


class DeliveryResult:
    def __init__(self, status: str, message_id: str = "") -> None:
        self.status = status  # sent | failed | logged
        self.message_id = message_id


class NotificationAdapter(Protocol):
    async def send_login_challenge(
        self,
        *,
        challenge_id: str,
        otp: str,
        approve_nonce: str,
        ip: str,
        user_agent: str,
    ) -> DeliveryResult: ...


class TestNotificationAdapter:
    """Logs the challenge locally in tests. Live mode refuses this adapter."""

    async def send_login_challenge(
        self,
        *,
        challenge_id: str,
        otp: str,
        approve_nonce: str,
        ip: str,
        user_agent: str,
    ) -> DeliveryResult:
        settings = get_settings()
        if settings.is_live:
            logger.error("test notification adapter invoked in live mode; challenge not delivered")
            return DeliveryResult("failed")
        logger.warning(
            "TEST-ONLY login challenge %s: OTP=%s (from %s). "
            "This line must never appear in live logs.",
            challenge_id,
            otp,
            ip or "unknown",
        )
        return DeliveryResult("logged")


class TelegramNotificationAdapter:
    """Delivers the login challenge to the authorized Telegram chat with
    approve/deny buttons plus the OTP as manual fallback. The OTP travels
    only inside the Telegram message, never through application logs."""

    api_base = "https://api.telegram.org"

    async def send_login_challenge(
        self,
        *,
        challenge_id: str,
        otp: str,
        approve_nonce: str,
        ip: str,
        user_agent: str,
    ) -> DeliveryResult:
        settings = get_settings()
        if not settings.telegram_bot_token or not settings.telegram_allowed_chat_id:
            logger.error("telegram adapter not configured; challenge %s not delivered", challenge_id)
            return DeliveryResult("failed")

        text = (
            "\U0001f510 Login request\n"
            f"Challenge: {challenge_id[:8]}\n"
            f"From IP: {ip or 'unknown'}\n"
            f"Device: {(user_agent or 'unknown')[:120]}\n\n"
            f"One-time code: {otp}\n"
            "Approve only if this is you."
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"login:approve:{approve_nonce}"},
                    {"text": "❌ Deny", "callback_data": f"login:deny:{approve_nonce}"},
                ]
            ]
        }
        url = f"{self.api_base}/bot{settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": settings.telegram_allowed_chat_id,
            "text": text,
            "reply_markup": keyboard,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(url, json=payload)
                body = response.json()
                if response.status_code == 200 and body.get("ok"):
                    message_id = str(body.get("result", {}).get("message_id", ""))
                    logger.info("login challenge %s delivered via telegram", challenge_id)
                    return DeliveryResult("sent", message_id)
                logger.error(
                    "telegram delivery failed for challenge %s: http %s",
                    challenge_id,
                    response.status_code,
                )
                return DeliveryResult("failed")
        except httpx.HTTPError as exc:
            logger.error(
                "telegram delivery error for challenge %s: %s",
                challenge_id,
                exc.__class__.__name__,
            )
            return DeliveryResult("failed")


def get_notification_adapter() -> NotificationAdapter:
    settings = get_settings()
    if settings.auth_notification_adapter == "telegram":
        return TelegramNotificationAdapter()
    return TestNotificationAdapter()
