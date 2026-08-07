from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.settings import get_settings
from app.db.models import AuthSession, User
from app.db.session import get_db
from app.domain.actors import Actor
from app.services.auth_service import resolve_session

SESSION_COOKIE = "hc_session"
LOGIN_FLOW_COOKIE = "hc_login"


def client_ip(request: Request) -> str:
    settings = get_settings()
    if settings.trusted_proxy_enabled:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")


@dataclass
class CurrentAuth:
    session: AuthSession
    user: User

    @property
    def actor(self) -> Actor:
        return Actor(kind="user", id=self.user.username, label=self.user.username)


async def require_auth(request: Request, db: AsyncSession = Depends(get_db)) -> CurrentAuth:
    token = request.cookies.get(SESSION_COOKIE, "")
    resolved = await resolve_session(db, token)
    if resolved is None:
        raise HTTPException(status_code=401, detail="authentication required")
    session, user = resolved
    return CurrentAuth(session=session, user=user)


async def require_csrf(request: Request, auth: CurrentAuth = Depends(require_auth)) -> CurrentAuth:
    """Authentication + CSRF check for state-changing browser requests."""
    header = request.headers.get("x-csrf-token", "")
    if not header or not security.constant_time_equals(header, auth.session.csrf_token):
        raise HTTPException(status_code=403, detail="invalid CSRF token")
    return auth
