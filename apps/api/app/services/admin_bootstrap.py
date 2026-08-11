"""Narrow, auditable first-account bootstrap for live installations."""

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.locks import try_advisory_xact_lock
from app.db.models import User
from app.domain.actors import Actor
from app.services.audit import write_audit
from app.services.auth_service import create_user


BOOTSTRAP_ACTOR = Actor(kind="system", id="admin-bootstrap", label="admin bootstrap")


@dataclass(frozen=True)
class AdminBootstrapResult:
    created: bool
    code: str
    username: str = ""
    recovery_codes: list[str] = field(default_factory=list)


def validate_bootstrap_input(username: str, password: str) -> tuple[str, str]:
    username = username.strip()
    if not 1 <= len(username) <= 64:
        raise ValueError("username must be between 1 and 64 characters")
    if any(ord(char) < 33 or ord(char) == 127 for char in username):
        raise ValueError("username must not contain whitespace or control characters")
    if not 12 <= len(password) <= 256:
        raise ValueError("password must be between 12 and 256 characters")
    return username, password


async def create_first_admin(
    db: AsyncSession,
    *,
    username: str,
    password: str,
) -> AdminBootstrapResult:
    """Create the only account allowed by the installation bootstrap path."""

    username, password = validate_bootstrap_input(username, password)
    if not await try_advisory_xact_lock(db, "homelab.auth.first_admin"):
        return AdminBootstrapResult(created=False, code="bootstrap_busy")

    count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    if count > 0:
        await write_audit(
            db,
            actor=BOOTSTRAP_ACTOR,
            source="cli",
            action="auth.admin.bootstrap",
            outcome="already_initialized",
            metadata={"username": username},
        )
        return AdminBootstrapResult(created=False, code="already_initialized")

    user, recovery_codes = await create_user(db, username, password)
    await write_audit(
        db,
        actor=BOOTSTRAP_ACTOR,
        source="cli",
        action="auth.admin.bootstrap",
        outcome="success",
        metadata={"username": user.username},
    )
    return AdminBootstrapResult(
        created=True,
        code="created",
        username=user.username,
        recovery_codes=recovery_codes,
    )
