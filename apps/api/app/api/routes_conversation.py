"""Conversation endpoints shared by Telegram and an optional future web chat."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAuth, require_auth, require_csrf
from app.db.session import get_db
from app.services.conversation_service import (
    ConversationError,
    confirm_pending_task,
    conversation_status,
    handle_conversation_message,
)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


class ConversationMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=64)
    task_id: str | None = Field(default=None, max_length=64)


@router.get("/status")
async def conversation_status_endpoint(auth: CurrentAuth = Depends(require_auth)) -> dict:
    return conversation_status().model_dump()


@router.post("/message")
async def conversation_message(
    payload: ConversationMessageRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        result = await handle_conversation_message(
            db,
            channel="web",
            user_ref=auth.user.id,
            content=payload.message,
            actor=auth.actor,
            conversation_id=payload.conversation_id,
            task_id=payload.task_id,
        )
    except ConversationError as exc:
        # Failed model turns are canonical delivery telemetry and remain useful
        # even when no assistant response can be produced.
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    await db.commit()
    return result.model_dump(mode="json")


@router.post("/task-proposals/{nonce}/confirm")
async def confirm_task_proposal(
    nonce: str,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task = await confirm_pending_task(db, nonce=nonce, actor=auth.actor, channel="web")
    except ConversationError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail={"code": "task_proposal_invalid", "message": str(exc)}) from exc
    await db.commit()
    return {
        "task_id": task.id,
        "title": task.title,
    }
