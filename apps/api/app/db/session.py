from collections.abc import AsyncIterator
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.settings import get_settings
from app.db.models import Base

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _database_url() -> str:
    url = get_settings().database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("DATABASE_URL must use PostgreSQL with the psycopg driver")
    return url


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        url = _database_url()
        kwargs = {
            "pool_size": max(1, settings.database_pool_size),
            "max_overflow": max(0, settings.database_max_overflow),
            "pool_timeout": max(1.0, settings.database_pool_timeout_seconds),
            "pool_recycle": max(60, settings.database_pool_recycle_seconds),
            "pool_pre_ping": True,
        }
        _engine = create_async_engine(url, **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


def _check_schema_drift(sync_conn) -> None:
    """Catch a database whose schema doesn't match the current models with
    an explicit message instead of surfacing obscure "no such column"
    errors at request time."""
    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    drift: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing_columns:
                drift.append(f"{table.name}.{column.name}")
    if drift:
        raise RuntimeError(
            "database schema is out of date, missing columns: "
            + ", ".join(sorted(drift))
            + ". Run `alembic upgrade head` (live) or recreate the "
            "database (test)."
        )


async def init_db() -> None:
    """Create test tables directly; live schema remains owned by Alembic."""
    engine = get_engine()
    async with engine.begin() as conn:
        if not get_settings().is_live:
            await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_check_schema_drift)


async def reset_engine_for_tests() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
