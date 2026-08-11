import importlib.util
import os
import sys
from pathlib import Path
from uuid import uuid4

# Test environment must be in place before any app module is imported:
# env vars take precedence over the repo .env file.
os.environ.update(
    {
        "APP_ENV": "test",
        "SESSION_SECRET": "test-secret-value-for-tests-0123456789",
        "AUTH_NOTIFICATION_ADAPTER": "test",
        "AUTH_LOGIN_MODE": "password",
        "AUTH_RECOVERY_ENABLED": "true",
        "LOGIN_CHALLENGE_TTL_SECONDS": "300",
        "LOGIN_CHALLENGE_MAX_ATTEMPTS": "3",
        "TELEGRAM_ALLOWED_USER_ID": "111",
        "TELEGRAM_ALLOWED_CHAT_ID": "222",
        "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
        "TELEGRAM_BOT_TOKEN": "",
        "FIXER_DISPATCH_ENABLED": "false",
        "FIXER_DISPATCH_SECRET": "",
        "CONVERSATION_ENABLED": "true",
        "CONVERSATION_PROVIDER": "ollama",
        "OPENCODE_GO_API_KEY": "",
        "TASK_ROUTER_PROVIDER": "",
        "WATCHERS_INTERVAL_SECONDS": "300",
        "HOMELAB_CONFIG_PATH": "config/homelab.test-missing.yml",
        "SECRETS_PATH": "config/secrets.test-missing.yml",
        "RUNBOOKS_CONFIG_PATH": "config/runbooks.test-missing.yml",
        "BOOTSTRAP_ADMIN_USERNAME": "",
        "BOOTSTRAP_ADMIN_PASSWORD": "",
        "AUDIT_JSONL_ENABLED": "false",
    }
)

import httpx
import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import URL, make_url

from app.core.settings import get_settings
from app.db.session import get_session_factory, init_db, reset_engine_for_tests
from app.main import create_app
from app.services.dependency_graph import clear_cache as clear_dependency_graph_cache
from app.services.inventory import clear_cache as clear_inventory_cache
from app.services.runbooks import clear_cache as clear_runbooks_cache
from app.services.watchers import reset_runtime_state_for_tests as reset_watchers_runtime_state
from app.services.topology_snapshot import clear_topology_snapshot_cache
from app.services import auth_service, rate_limit
from app.services.model_providers import seed_model_profiles


class CaptureAdapter:
    """Notification adapter that captures the OTP and nonce for tests."""

    def __init__(self) -> None:
        self.otp = ""
        self.nonce = ""
        self.challenge_id = ""

    async def send_login_challenge(self, *, challenge_id, otp, approve_nonce, ip, user_agent):
        from app.services.notifications import DeliveryResult

        self.challenge_id = challenge_id
        self.otp = otp
        self.nonce = approve_nonce
        return DeliveryResult("sent", "msg-1")


def _test_database_url() -> URL:
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError(
            "PostgreSQL tests require TEST_DATABASE_URL; it is used only as an admin connection "
            "for randomly named temporary databases"
        )
    url = make_url(raw)
    if not url.drivername.startswith("postgresql"):
        raise RuntimeError("TEST_DATABASE_URL must point to PostgreSQL")
    return url.set(drivername="postgresql+psycopg")


class TemporaryPostgres:
    def __init__(self, base_url: URL):
        self.base_url = base_url
        self.admin_url = base_url.set(drivername="postgresql", database="postgres")
        self.created: set[str] = set()

    def _connect(self):
        try:
            return psycopg.connect(
                self.admin_url.render_as_string(hide_password=False), autocommit=True
            )
        except psycopg.OperationalError:
            raise RuntimeError(
                "cannot connect to the isolated PostgreSQL test server"
            ) from None

    def create(self, *, template: str | None = None) -> tuple[str, str]:
        name = f"hc_test_{uuid4().hex}"
        with self._connect() as conn:
            if template:
                conn.execute(
                    sql.SQL("CREATE DATABASE {} WITH TEMPLATE {}").format(
                        sql.Identifier(name), sql.Identifier(template)
                    )
                )
            else:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        self.created.add(name)
        url = self.base_url.set(database=name).render_as_string(hide_password=False)
        return name, url

    def drop(self, name: str) -> None:
        if name not in self.created:
            return
        with self._connect() as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        self.created.discard(name)

    def close(self) -> None:
        for name in tuple(self.created):
            self.drop(name)


@pytest.fixture(scope="session")
def postgres_databases():
    manager = TemporaryPostgres(_test_database_url())
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture(scope="session")
async def _database_template(postgres_databases):
    """Build immutable seed data once, then clone it for every test."""
    template_name, template_url = postgres_databases.create()
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = template_url
    get_settings.cache_clear()
    await reset_engine_for_tests()

    try:
        await init_db()
        async with get_session_factory()() as db:
            await seed_model_profiles(db)
            await db.commit()
        await reset_engine_for_tests()
        yield template_name
    finally:
        await reset_engine_for_tests()
        postgres_databases.drop(template_name)
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _fresh_environment(monkeypatch, _database_template, postgres_databases):
    database_name, database_url = postgres_databases.create(template=_database_template)
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    clear_inventory_cache()
    clear_dependency_graph_cache()
    clear_runbooks_cache()
    reset_watchers_runtime_state()
    clear_topology_snapshot_cache()
    await reset_engine_for_tests()
    rate_limit.limiter.reset()

    # Keep real local credentials out of the test run, for every provider
    # client module (each imports the loaders into its own namespace).
    for module in (
        "app.providers.proxmox.client",
        "app.providers.opnsense.client",
        "app.providers.pbs.client",
        "app.providers.homeassistant.client",
        "app.providers.frigate.client",
        "app.providers.fritzbox.client",
        "app.providers.adguard.client",
        "app.providers.nextcloud.client",
        "app.providers.mikrotik.client",
        "app.providers.asterisk.client",
        "app.providers.uptimekuma.client",
        "app.providers.emqx.client",
        "app.providers.vps.client",
    ):
        monkeypatch.setattr(f"{module}.load_credentials_env", lambda: {}, raising=True)
        monkeypatch.setattr(f"{module}.get_provider_secrets", lambda _pid: {}, raising=True)

    yield

    await reset_engine_for_tests()
    get_settings.cache_clear()
    clear_inventory_cache()
    clear_dependency_graph_cache()
    clear_runbooks_cache()
    reset_watchers_runtime_state()
    clear_topology_snapshot_cache()
    postgres_databases.drop(database_name)


@pytest.fixture
def empty_postgres_database(postgres_databases):
    name, url = postgres_databases.create()
    try:
        yield url
    finally:
        postgres_databases.drop(name)


@pytest.fixture
def capture_adapter(monkeypatch) -> CaptureAdapter:
    adapter = CaptureAdapter()
    monkeypatch.setattr(
        "app.services.auth_service.get_notification_adapter", lambda: adapter, raising=True
    )
    return adapter


@pytest.fixture
async def db_session():
    async with get_session_factory()() as session:
        yield session


@pytest.fixture
async def user(db_session):
    user, codes = await auth_service.create_user(db_session, "operator", "correct-horse-battery")
    await db_session.commit()
    setattr(user, "plain_password", "correct-horse-battery")
    setattr(user, "recovery_codes", codes)
    return user


@pytest.fixture
async def client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def do_login(client, capture_adapter, username="operator", password="correct-horse-battery"):
    """First factor + OTP second factor. Returns (response_json, csrf_token)."""
    response = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    challenge_id = response.json()["challenge_id"]
    verify = await client.post(
        "/api/auth/verify-otp", json={"challenge_id": challenge_id, "otp": capture_adapter.otp}
    )
    assert verify.status_code == 200, verify.text
    body = verify.json()
    return body, body["csrf_token"]


MCP_SERVER_PATH = Path(__file__).resolve().parents[3] / "apps" / "mcp" / "server.py"


def load_mcp_server_module():
    """Load apps/mcp/server.py as a fresh module. It isn't a package under
    apps/api, so it can't be imported normally in tests."""
    spec = importlib.util.spec_from_file_location("homelab_mcp_server", MCP_SERVER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["homelab_mcp_server"] = module
    spec.loader.exec_module(module)
    return module


async def as_mcp_agent(server, monkeypatch, agent_id: str):
    """Switch the loaded MCP server module to act as the given agent
    (claude|codex) for subsequent handle_call_tool calls."""
    monkeypatch.setenv("MCP_AGENT_ID", agent_id)
    server.get_settings.cache_clear()
