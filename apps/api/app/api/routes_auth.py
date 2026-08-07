from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    LOGIN_FLOW_COOKIE,
    SESSION_COOKIE,
    CurrentAuth,
    client_ip,
    require_auth,
    require_csrf,
    user_agent,
)
from app.core.settings import get_settings
from app.db.session import get_db
from app.services import rate_limit
from app.services.auth_service import (
    AuthError,
    challenge_public_status,
    complete_approved_challenge,
    create_login_challenge,
    get_challenge,
    recovery_login,
    revoke_session,
    validate_first_factor,
    validate_login_identity,
    verify_otp,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str | None = Field(default=None, min_length=1, max_length=256)


class ChallengeRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(min_length=8, max_length=64)


class OtpRequest(ChallengeRef):
    otp: str = Field(min_length=4, max_length=16)


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    recovery_code: str = Field(min_length=8, max_length=32)


def _set_session_cookie(response: Response, raw_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure_flag,
        samesite="strict",
        path="/",
    )


def _clear_cookie(response: Response, name: str) -> None:
    settings = get_settings()
    response.delete_cookie(name, path="/", secure=settings.cookie_secure_flag, httponly=True)


def _auth_payload(auth_user, csrf_token: str) -> dict:
    return {
        "authenticated": True,
        "user": {"id": auth_user.id, "username": auth_user.username},
        "csrf_token": csrf_token,
    }


@router.get("/config")
def auth_config(response: Response) -> dict:
    settings = get_settings()
    response.headers["Cache-Control"] = "no-store"
    return {
        "app_name": settings.app_name,
        "environment": settings.app_env,
        "login_mode": settings.auth_login_mode,
        "methods": {
            "telegram_approval": settings.auth_notification_adapter == "telegram",
            "otp_fallback": True,
            "recovery": settings.auth_recovery_enabled
            and settings.auth_login_mode == "password",
        },
    }


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    ip = client_ip(request)
    if not rate_limit.check("login.attempt", ip or "unknown") or not rate_limit.check(
        "login.failure", payload.username.lower()
    ):
        raise HTTPException(status_code=429, detail="too many attempts, retry later")

    settings = get_settings()
    try:
        if settings.auth_login_mode == "telegram_only":
            if payload.password is not None:
                raise HTTPException(
                    status_code=400,
                    detail="password is not accepted in telegram-only mode",
                )
            user = await validate_login_identity(db, payload.username, ip)
        else:
            if payload.password is None:
                raise AuthError("invalid_credentials", "invalid credentials")
            user = await validate_first_factor(
                db, payload.username, payload.password, ip, user_agent(request)
            )
    except AuthError:
        await db.commit()
        raise HTTPException(status_code=401, detail="invalid credentials")

    if not rate_limit.check("login.challenge", user.id):
        await db.commit()
        raise HTTPException(status_code=429, detail="too many challenges, retry later")

    challenge, flow_token, _otp = await create_login_challenge(db, user, ip, user_agent(request))
    await db.commit()

    response.set_cookie(
        LOGIN_FLOW_COOKIE,
        flow_token,
        max_age=settings.login_challenge_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure_flag,
        samesite="strict",
        path="/api/auth",
    )
    return {
        "challenge_id": challenge.id,
        "expires_at": challenge.expires_at,
        "delivery_status": challenge.delivery_status,
        "methods": ["approval", "otp"],
    }


@router.get("/challenge/{challenge_id}")
async def challenge_status(challenge_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    challenge = await get_challenge(db, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="unknown challenge")
    return {"status": challenge_public_status(challenge), "expires_at": challenge.expires_at}


@router.post("/complete")
async def complete(
    payload: ChallengeRef,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    flow_token = request.cookies.get(LOGIN_FLOW_COOKIE, "")
    try:
        user, session, raw_token = await complete_approved_challenge(
            db, payload.challenge_id, flow_token, client_ip(request), user_agent(request)
        )
    except AuthError as exc:
        await db.commit()
        status = 410 if exc.code == "expired" else 401
        raise HTTPException(status_code=status, detail=exc.message)
    await db.commit()

    _set_session_cookie(response, raw_token)
    _clear_cookie(response, LOGIN_FLOW_COOKIE)
    return _auth_payload(user, session.csrf_token)


@router.post("/verify-otp")
async def verify_otp_endpoint(
    payload: OtpRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    ip = client_ip(request)
    if not rate_limit.check("login.otp", ip or "unknown"):
        raise HTTPException(status_code=429, detail="too many attempts, retry later")

    flow_token = request.cookies.get(LOGIN_FLOW_COOKIE, "")
    try:
        user, session, raw_token = await verify_otp(
            db, payload.challenge_id, payload.otp, flow_token, ip, user_agent(request)
        )
    except AuthError as exc:
        await db.commit()
        status = 410 if exc.code == "expired" else 401
        raise HTTPException(status_code=status, detail=exc.message)
    await db.commit()

    _set_session_cookie(response, raw_token)
    _clear_cookie(response, LOGIN_FLOW_COOKIE)
    return _auth_payload(user, session.csrf_token)


@router.post("/recovery")
async def recovery(
    payload: RecoveryRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    settings = get_settings()
    if (
        not settings.auth_recovery_enabled
        or settings.auth_login_mode == "telegram_only"
    ):
        raise HTTPException(status_code=404, detail="recovery is disabled")

    ip = client_ip(request)
    if not rate_limit.check("login.recovery", ip or "unknown"):
        raise HTTPException(status_code=429, detail="too many attempts, retry later")

    try:
        user, session, raw_token = await recovery_login(
            db, payload.username, payload.password, payload.recovery_code, ip, user_agent(request)
        )
    except AuthError:
        await db.commit()
        raise HTTPException(status_code=401, detail="invalid credentials or recovery code")
    await db.commit()

    _set_session_cookie(response, raw_token)
    return _auth_payload(user, session.csrf_token)


@router.get("/session")
async def session_info(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    from app.services.auth_service import resolve_session

    resolved = await resolve_session(db, request.cookies.get(SESSION_COOKIE, ""))
    if resolved is None:
        return {"authenticated": False}
    session, user = resolved
    return {**_auth_payload(user, session.csrf_token), "expires_at": session.expires_at}


@router.post("/logout", status_code=204)
async def logout(
    response: Response,
    auth: CurrentAuth = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> None:
    await revoke_session(db, auth.session, auth.user)
    await db.commit()
    _clear_cookie(response, SESSION_COOKIE)
