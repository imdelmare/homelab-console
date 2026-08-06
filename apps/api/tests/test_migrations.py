from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.core.settings import get_settings
from app.db.models import Base

API_ROOT = Path(__file__).resolve().parents[1]


def test_alembic_upgrade_head_creates_full_schema(empty_postgres_database, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", empty_postgres_database)
    get_settings.cache_clear()
    try:
        config = Config(str(API_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(API_ROOT / "migrations"))
        command.upgrade(config, "head")

        engine = create_engine(empty_postgres_database)
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            router_columns = {
                column["name"] for column in inspector.get_columns("task_router_jobs")
            }
            router_indexes = {
                index["name"]: index for index in inspector.get_indexes("task_router_jobs")
            }
        finally:
            engine.dispose()

        expected = {table.name for table in Base.metadata.sorted_tables}
        assert expected <= tables
        assert "alembic_version" in tables
        assert {"task_id", "task_version", "lease_token", "policy_context"} <= router_columns
        assert router_indexes["ix_task_router_jobs_task_id"]["unique"] is True
        assert "ix_task_router_jobs_status_available" in router_indexes
    finally:
        get_settings.cache_clear()
