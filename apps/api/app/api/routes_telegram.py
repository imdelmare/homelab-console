import asyncio
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.settings import get_settings
from app.db.session import get_db
from app.services.telegram_service import (
    answer_callback_query_result,
    edit_message_result,
    handle_update,
    send_message_result,
)

router = APIRouter(prefix="/api/telegram", tags=["telegram"])
_UPDATE_CACHE_SIZE = 512
TelegramDelivery = tuple[str, str | dict, str]
_processed_update_ids: OrderedDict[str, TelegramDelivery | None] = OrderedDict()
_update_locks: dict[str, asyncio.Lock] = {}


async def _send_reply(chat_id: int | str, reply: str | dict) -> bool:
    if isinstance(reply, dict):
        text = str(reply.get("text") or "")
        markup = reply.get("reply_markup")
        ok, _, _ = await send_message_result(
            str(chat_id),
            text,
            markup if isinstance(markup, dict) else None,
        )
        return ok
    ok, _, _ = await send_message_result(str(chat_id), reply)
    return ok


async def _process_update(db: AsyncSession, update: dict) -> TelegramDelivery | None:
    callback = update.get("callback_query") or {}
    callback_message_id = str((((callback.get("message")) or {}).get("message_id")) or "")
    callback_query_id = str(callback.get("id") or "")
    if callback_query_id:
        await answer_callback_query_result(callback_query_id)

    reply = await handle_update(db, update)
    await db.commit()

    if not reply:
        return None
    chat_id = (
        ((update.get("message") or {}).get("chat") or {}).get("id")
        or ((((update.get("callback_query") or {}).get("message")) or {}).get("chat") or {}).get("id")
    )
    if chat_id is None:
        return None
    return str(chat_id), reply, callback_message_id


async def _deliver_reply(delivery: TelegramDelivery | None) -> bool:
    if delivery is None:
        return True
    chat_id, reply, callback_message_id = delivery
    send_new = bool(isinstance(reply, dict) and reply.get("send_new"))
    if callback_message_id and not send_new:
        text = str(reply.get("text") or "") if isinstance(reply, dict) else reply
        reply_markup = reply.get("reply_markup") if isinstance(reply, dict) else None
        if not isinstance(reply_markup, dict):
            reply_markup = {"inline_keyboard": []}
        edited, _ = await edit_message_result(
            chat_id,
            callback_message_id,
            text,
            reply_markup if isinstance(reply_markup, dict) else None,
        )
        if edited:
            return True
    return await _send_reply(chat_id, reply)


def _remember_update(update_id: str, delivery: TelegramDelivery | None) -> None:
    _processed_update_ids[update_id] = delivery
    _processed_update_ids.move_to_end(update_id)
    while len(_processed_update_ids) > _UPDATE_CACHE_SIZE:
        expired, _ = _processed_update_ids.popitem(last=False)
        _update_locks.pop(expired, None)


def _mark_delivered(update_id: str) -> None:
    if update_id in _processed_update_ids:
        _processed_update_ids[update_id] = None


@router.post("/webhook")
async def telegram_webhook(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    settings = get_settings()
    if not settings.telegram_webhook_secret:
        raise HTTPException(status_code=404, detail="webhook not configured")

    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not security.constant_time_equals(header, settings.telegram_webhook_secret):
        raise HTTPException(status_code=403, detail="invalid webhook secret")

    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid payload")
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="invalid payload")

    raw_update_id = update.get("update_id")
    if not isinstance(raw_update_id, int):
        delivery = await _process_update(db, update)
        if not await _deliver_reply(delivery):
            raise HTTPException(status_code=503, detail="telegram delivery failed")
        return {}

    update_id = str(raw_update_id)
    lock = _update_locks.setdefault(update_id, asyncio.Lock())
    async with lock:
        if update_id in _processed_update_ids:
            delivery = _processed_update_ids[update_id]
            if await _deliver_reply(delivery):
                _mark_delivered(update_id)
                return {}
            raise HTTPException(status_code=503, detail="telegram delivery failed")

        # Processing and its database commit happen once. Remember the final
        # delivery before contacting Telegram so retries can resend the reply
        # without replaying conversation or tool side effects.
        delivery = await _process_update(db, update)
        _remember_update(update_id, delivery)
        if await _deliver_reply(delivery):
            _mark_delivered(update_id)
            return {}
        raise HTTPException(status_code=503, detail="telegram delivery failed")
