from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAuth, require_auth, require_csrf
from app.db.session import get_db
from app.services.watchers import (
    configure_watcher,
    incident_public,
    list_incidents,
    list_watcher_runs,
    resolve_incident_as_handled,
    run_watchers,
    set_watcher_automation_enabled,
    watcher_status,
    watcher_run_public,
)
from app.services.tasks_service import TaskServiceError

router = APIRouter(prefix="/api/watchers", tags=["watchers"])


class RunWatchersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watcher_ids: list[str] = Field(default_factory=list, max_length=8)


class ResolveHandledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=1000)


class WatcherAutomationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class WatcherConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=86400)
    min_severity: str | None = Field(default=None, max_length=16)
    investigation_mode: str | None = Field(default=None, max_length=24)


def _watcher_http_error(exc: TaskServiceError) -> HTTPException:
    status = {"unknown_incident": 404, "unknown_watcher": 404}.get(exc.code, 400)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


@router.get("/status")
async def status(
    auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await watcher_status(db)


@router.post("/automation")
async def automation(
    payload: WatcherAutomationRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await set_watcher_automation_enabled(payload.enabled, db, actor=auth.actor)
    await db.commit()
    return result


@router.patch("/config/{watcher_id}")
async def watcher_config_endpoint(
    watcher_id: str,
    payload: WatcherConfigRequest,
    _auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        result = await configure_watcher(
            db,
            watcher_id,
            enabled=payload.enabled,
            interval_seconds=payload.interval_seconds,
            min_severity=payload.min_severity,
            investigation_mode=payload.investigation_mode,
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _watcher_http_error(exc) from exc
    await db.commit()
    return result


@router.get("/incidents")
async def incidents(
    status: str | None = Query(default="open", max_length=16),
    limit: int = Query(default=100, ge=1, le=100),
    auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = await list_incidents(db, status=status, limit=limit)
    return [incident_public(row) for row in rows]


@router.post("/incidents/{incident_id}/resolve-handled")
async def resolve_incident_handled_endpoint(
    incident_id: str,
    payload: ResolveHandledRequest,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        incident = await resolve_incident_as_handled(
            db,
            incident_id=incident_id,
            actor=auth.actor,
            note=payload.note,
        )
    except TaskServiceError as exc:
        await db.rollback()
        raise _watcher_http_error(exc) from exc
    await db.commit()
    return incident_public(incident)


@router.get("/runs")
async def watcher_runs(
    limit: int = Query(default=50, ge=1, le=100),
    auth: CurrentAuth = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = await list_watcher_runs(db, limit=limit)
    return [watcher_run_public(row) for row in rows]


@router.post("/run")
async def run_watchers_endpoint(
    payload: RunWatchersRequest,
    _auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await run_watchers(
        db,
        watcher_ids=set(payload.watcher_ids) if payload.watcher_ids else None,
    )
    if not result.get("ok") and result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    await db.commit()
    return result
