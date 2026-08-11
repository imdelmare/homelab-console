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
            mcp_client_columns = {
                column["name"] for column in inspector.get_columns("mcp_clients")
            }
            mcp_client_indexes = {
                index["name"]: index for index in inspector.get_indexes("mcp_clients")
            }
            worker_columns = {
                column["name"] for column in inspector.get_columns("task_worker_jobs")
            }
            worker_indexes = {
                index["name"]: index for index in inspector.get_indexes("task_worker_jobs")
            }
            approval_columns = {column["name"] for column in inspector.get_columns("approvals")}
            idempotency_columns = {
                column["name"] for column in inspector.get_columns("task_worker_idempotency")
            }
        finally:
            engine.dispose()

        expected = {table.name for table in Base.metadata.sorted_tables}
        assert expected <= tables
        assert "alembic_version" in tables
        assert {"task_id", "task_version", "lease_token", "policy_context"} <= router_columns
        assert router_indexes["ix_task_router_jobs_task_id"]["unique"] is True
        assert "ix_task_router_jobs_status_available" in router_indexes
        assert {"capabilities", "principal_id"} <= mcp_client_columns
        assert mcp_client_indexes["ux_mcp_clients_principal_id_nonempty"]["unique"] is True
        assert {"client_id", "principal_id", "lease_token_hash", "lease_generation"} <= worker_columns
        assert worker_indexes["ux_task_worker_jobs_active_task"]["unique"] is True
        assert "ix_task_worker_jobs_client_status_available" in worker_indexes
        assert {"worker_job_id", "worker_lease_generation"} <= approval_columns
        assert {"job_id", "lease_generation", "operation", "idempotency_key", "result"} <= idempotency_columns
    finally:
        get_settings.cache_clear()
