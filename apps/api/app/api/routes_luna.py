from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAuth, require_auth, require_csrf
from app.db.session import get_db
from app.services.luna_metrics import luna_metrics, review_task_router
from app.services.task_router import (
    TASK_ROUTER_ACTIONS,
    TASK_ROUTER_CATEGORIES,
    TASK_ROUTER_OWNERS,
    TASK_ROUTER_PRIORITIES,
    TASK_ROUTER_SEVERITIES,
)
from app.services.tasks_service import TaskServiceError

router = APIRouter(prefix="/api/luna", tags=["luna"])


class RouterCorrections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str | None = Field(default=None, max_length=32)
    category: str | None = Field(default=None, max_length=32)
    priority: str | None = Field(default=None, max_length=16)
    severity: str | None = Field(default=None, max_length=16)
    suggested_owner: str | None = Field(default=None, max_length=16)
    runbook: str | None = Field(default=None, max_length=128)
    needs_operator: bool | None = None


class RouterReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str = Field(max_length=16)
    corrections: RouterCorrections = Field(default_factory=RouterCorrections)
    note: str = Field(default="", max_length=1000)


def _validate_corrections(corrections: RouterCorrections) -> dict:
    values = corrections.model_dump(exclude_none=True)
    allowed = {
        "action": TASK_ROUTER_ACTIONS,
        "category": TASK_ROUTER_CATEGORIES,
        "priority": TASK_ROUTER_PRIORITIES,
        "severity": TASK_ROUTER_SEVERITIES,
        "suggested_owner": TASK_ROUTER_OWNERS,
    }
    for key, choices in allowed.items():
        if key in values and values[key] not in choices:
            raise HTTPException(status_code=400, detail=f"invalid corrected {key}")
    return values


@router.get("/metrics")
async def metrics(
    days: int = Query(default=30, ge=1, le=365),
    _auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await luna_metrics(db, days=days)


@router.post("/tasks/{task_id}/review")
async def review(
    task_id: str,
    payload: RouterReviewRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    corrections = _validate_corrections(payload.corrections)
    if payload.verdict == "corrected" and not corrections:
        raise HTTPException(status_code=400, detail="corrected reviews require at least one correction")
    try:
        row = await review_task_router(
            db,
            task_id=task_id,
            verdict=payload.verdict,
            corrections=corrections,
            note=payload.note,
            actor=auth.actor,
        )
    except TaskServiceError as exc:
        await db.rollback()
        status = 404 if exc.code == "unknown_task" else 400
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message}) from exc
    await db.commit()
    return {
        "task_id": row.task_id,
        "verdict": row.verdict,
        "corrections": row.corrections,
        "note": row.note,
        "reviewed_by": row.reviewed_by,
        "updated_at": row.updated_at,
    }
