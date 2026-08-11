import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from app.api import routes_auth, routes_control, routes_conversation, routes_luna, routes_telegram, routes_watchers
from app.core.settings import get_settings
from app.db.models import User
from app.db.session import get_session_factory, init_db
from app.services.auth_service import create_user
from app.services.inventory import config_path
from app.services.model_providers import seed_model_profiles
from app.services.luna_metrics import backfill_llm_usage
from app.services.notification_outbox import worker_loop as notification_worker_loop
from app.services.ops_health import retention_loop
from app.services.sentinel_heartbeat import (
    validate_configuration as validate_sentinel_heartbeat,
    worker_loop as sentinel_heartbeat_worker_loop,
)
from app.services.task_router_queue import worker_loop as task_router_worker_loop
from app.services.remediation_workers import recovery_loop as remediation_worker_recovery_loop
from app.services.watchers import watcher_scheduler_loop

logger = logging.getLogger("homelab")

WEAK_SECRETS = ("", "change-me", "change-me-with-a-long-random-value")


def _enforce_live_guards() -> None:
    settings = get_settings()
    if not settings.is_live:
        return
    if settings.session_secret in WEAK_SECRETS or len(settings.session_secret) < 32:
        raise RuntimeError("SESSION_SECRET must be a long random value in live mode")
    if settings.auth_notification_adapter == "test":
        raise RuntimeError(
            "AUTH_NOTIFICATION_ADAPTER=test is not allowed in live mode; configure telegram"
        )
    if settings.bootstrap_admin_password:
        raise RuntimeError(
            "BOOTSTRAP_ADMIN_PASSWORD must not be set in live mode; "
            "bootstrap the account via a controlled migration"
        )
    if not config_path().is_file():
        raise RuntimeError(
            f"HOMELAB_CONFIG_PATH ({settings.homelab_config_path}) does not exist; "
            "copy config/homelab.example.yml to a local file (e.g. "
            "config/homelab.local.yml) and point HOMELAB_CONFIG_PATH at it"
        )


async def _bootstrap_admin() -> None:
    settings = get_settings()
    if not settings.bootstrap_admin_username or not settings.bootstrap_admin_password:
        return
    async with get_session_factory()() as db:
        count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        if count > 0:
            return
        user, recovery_codes = await create_user(
            db, settings.bootstrap_admin_username, settings.bootstrap_admin_password
        )
        await db.commit()
        # Recovery codes are shown exactly once during controlled bootstrap.
        logger.warning(
            "BOOTSTRAP: created user '%s'. Recovery codes (store them safely, "
            "they will not be shown again):\n%s",
            user.username,
            "\n".join(f"  {code}" for code in recovery_codes),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _enforce_live_guards()
    if settings.sentinel_heartbeat_enabled:
        validate_sentinel_heartbeat()
    await init_db()
    async with get_session_factory()() as db:
        await seed_model_profiles(db)
        await backfill_llm_usage(db)
        await db.commit()
    await _bootstrap_admin()
    watcher_task = asyncio.create_task(watcher_scheduler_loop())
    notification_task = asyncio.create_task(notification_worker_loop())
    retention_task = asyncio.create_task(retention_loop())
    sentinel_heartbeat_task = (
        asyncio.create_task(sentinel_heartbeat_worker_loop())
        if settings.sentinel_heartbeat_enabled
        else None
    )
    task_router_task = (
        asyncio.create_task(task_router_worker_loop())
        if settings.conversation_enabled and settings.task_router_enabled
        else None
    )
    remediation_recovery_task = asyncio.create_task(remediation_worker_recovery_loop())
    try:
        yield
    finally:
        if watcher_task is not None:
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass
        notification_task.cancel()
        try:
            await notification_task
        except asyncio.CancelledError:
            pass
        retention_task.cancel()
        try:
            await retention_task
        except asyncio.CancelledError:
            pass
        if task_router_task is not None:
            task_router_task.cancel()
            try:
                await task_router_task
            except asyncio.CancelledError:
                pass
        if sentinel_heartbeat_task is not None:
            sentinel_heartbeat_task.cancel()
            try:
                await sentinel_heartbeat_task
            except asyncio.CancelledError:
                pass
        remediation_recovery_task.cancel()
        try:
            await remediation_recovery_task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["content-type", "x-csrf-token"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def readiness():
        try:
            async with get_session_factory()() as db:
                await db.execute(text("select 1"))
        except Exception:
            logger.exception("readiness check failed")
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "database": "unavailable"},
            )
        return {"status": "ready", "database": "ok"}

    app.include_router(routes_auth.router)
    app.include_router(routes_control.router)
    app.include_router(routes_conversation.router)
    app.include_router(routes_luna.router)
    app.include_router(routes_telegram.router)
    app.include_router(routes_watchers.router)
    return app


app = create_app()
