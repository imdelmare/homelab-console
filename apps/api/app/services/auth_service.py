"""Human authentication, Telegram challenges, sessions and recovery codes."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.settings import get_settings
from app.db.models import AuthSession, LoginChallenge, RecoveryCode, User, utcnow
from app.domain.actors import Actor
from app.services.audit import write_audit
from app.services.notifications import get_notification_adapter


class AuthError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _actor_for(user: User) -> Actor:
    return Actor(kind="user", id=user.username, label=user.username)


ANON = Actor(kind="system", id="anonymous", label="unauthenticated")


def _aware(value: datetime | None) -> datetime | None:
    """Normalize database datetimes to UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def create_user(db: AsyncSession, username: str, password: str) -> tuple[User, list[str]]:
    user = User(username=username, password_hash=security.hash_password(password))
    db.add(user)
    await db.flush()
    codes = security.generate_recovery_codes()
    for code in codes:
        db.add(RecoveryCode(user_id=user.id, code_hash=security.hash_token(code)))
    await db.flush()
    return user, codes


async def validate_first_factor(
    db: AsyncSession, username: str, password: str, ip: str, user_agent: str
) -> User:
    user = await get_user_by_username(db, username)
    # Verify against a dummy hash when the user is unknown to keep timing flat.
    if user is None:
        security.verify_password(security.hash_password("invalid"), password)
        await write_audit(
            db, actor=ANON, source="rest", action="auth.password.failed",
            outcome="unknown_user", metadata={"ip": ip},
        )
        raise AuthError("invalid_credentials", "invalid credentials")

    if user.disabled or not security.verify_password(user.password_hash, password):
        await write_audit(
            db, actor=_actor_for(user), source="rest", action="auth.password.failed",
            outcome="disabled" if user.disabled else "wrong_password", metadata={"ip": ip},
        )
        raise AuthError("invalid_credentials", "invalid credentials")

    await write_audit(
        db, actor=_actor_for(user), source="rest", action="auth.password.ok",
        outcome="success", metadata={"ip": ip},
    )
    return user


async def validate_login_identity(
    db: AsyncSession, username: str, ip: str
) -> User:
    """Resolve an active account before a Telegram-only challenge."""
    user = await get_user_by_username(db, username)
    if user is None or user.disabled:
        await write_audit(
            db,
            actor=ANON if user is None else _actor_for(user),
            source="rest",
            action="auth.identity.failed",
            outcome="unknown_user" if user is None else "disabled",
            metadata={"ip": ip},
        )
        raise AuthError("invalid_credentials", "invalid credentials")

    await write_audit(
        db,
        actor=_actor_for(user),
        source="rest",
        action="auth.identity.ok",
        outcome="success",
        metadata={"ip": ip},
    )
    return user


async def create_login_challenge(
    db: AsyncSession, user: User, ip: str, user_agent: str
) -> tuple[LoginChallenge, str, str]:
    """Create a second-factor challenge. Returns (challenge, flow_token, otp).

    Any previous pending challenge for the user is invalidated: a new
    challenge replaces the old OTP.
    """
    settings = get_settings()

    result = await db.execute(
        select(LoginChallenge).where(
            LoginChallenge.user_id == user.id, LoginChallenge.status == "pending"
        )
    )
    for previous in result.scalars():
        previous.status = "expired"

    otp = security.generate_otp()
    approve_nonce = security.generate_token()
    flow_token = security.generate_token()

    challenge = LoginChallenge(
        user_id=user.id,
        status="pending",
        expires_at=utcnow() + timedelta(seconds=settings.login_challenge_ttl_seconds),
        max_attempts=settings.login_challenge_max_attempts,
        approve_nonce_hash=security.hash_token(approve_nonce),
        flow_token_hash=security.hash_token(flow_token),
        ip=ip,
        user_agent=user_agent[:250],
    )
    db.add(challenge)
    await db.flush()
    challenge.otp_hash = security.otp_hash(challenge.id, otp)

    delivery = await get_notification_adapter().send_login_challenge(
        challenge_id=challenge.id,
        otp=otp,
        approve_nonce=approve_nonce,
        ip=ip,
        user_agent=user_agent,
    )
    challenge.delivery_status = delivery.status
    challenge.telegram_message_id = delivery.message_id

    await write_audit(
        db, actor=_actor_for(user), source="rest", action="auth.challenge.created",
        outcome=delivery.status, metadata={"challenge_id": challenge.id, "ip": ip},
    )
    return challenge, flow_token, otp


async def get_challenge(db: AsyncSession, challenge_id: str) -> LoginChallenge | None:
    return await db.get(LoginChallenge, challenge_id)


def challenge_public_status(challenge: LoginChallenge) -> str:
    expires_at = _aware(challenge.expires_at)
    if challenge.status == "pending" and (expires_at is None or expires_at < utcnow()):
        return "expired"
    return challenge.status


def _check_flow_token(challenge: LoginChallenge, flow_token: str) -> None:
    if not flow_token or not security.constant_time_equals(
        challenge.flow_token_hash, security.hash_token(flow_token)
    ):
        raise AuthError("invalid_challenge", "challenge does not belong to this browser")


async def verify_otp(
    db: AsyncSession, challenge_id: str, otp: str, flow_token: str, ip: str, user_agent: str
) -> tuple[User, AuthSession, str]:
    challenge = await get_challenge(db, challenge_id)
    if challenge is None:
        raise AuthError("invalid_challenge", "unknown challenge")

    _check_flow_token(challenge, flow_token)

    if challenge.status not in ("pending", "approved"):
        raise AuthError("invalid_challenge", "challenge is no longer usable")
    if (expires_at := _aware(challenge.expires_at)) is None or expires_at < utcnow():
        challenge.status = "expired"
        raise AuthError("expired", "challenge expired")
    if challenge.attempt_count >= challenge.max_attempts:
        challenge.status = "expired"
        raise AuthError("too_many_attempts", "too many attempts")

    challenge.attempt_count += 1
    user = await db.get(User, challenge.user_id)

    if not security.constant_time_equals(
        challenge.otp_hash, security.otp_hash(challenge.id, otp)
    ):
        await write_audit(
            db, actor=_actor_for(user) if user else ANON, source="rest",
            action="auth.otp.failed", outcome="wrong_otp",
            metadata={"challenge_id": challenge.id, "attempt": challenge.attempt_count},
        )
        if challenge.attempt_count >= challenge.max_attempts:
            challenge.status = "expired"
        raise AuthError("invalid_otp", "invalid code")

    if user is None or user.disabled:
        raise AuthError("invalid_credentials", "account unavailable")

    challenge.status = "consumed"
    challenge.approved_at = utcnow()
    challenge.consumed_at = utcnow()

    session, raw_token = await create_session(db, user, ip, user_agent)
    await write_audit(
        db, actor=_actor_for(user), source="rest", action="auth.login",
        outcome="success", metadata={"challenge_id": challenge.id, "factor": "otp", "ip": ip},
    )
    return user, session, raw_token


async def complete_approved_challenge(
    db: AsyncSession, challenge_id: str, flow_token: str, ip: str, user_agent: str
) -> tuple[User, AuthSession, str]:
    challenge = await get_challenge(db, challenge_id)
    if challenge is None:
        raise AuthError("invalid_challenge", "unknown challenge")

    _check_flow_token(challenge, flow_token)

    if challenge.status != "approved":
        raise AuthError("not_approved", "challenge is not approved")
    if (expires_at := _aware(challenge.expires_at)) is None or expires_at < utcnow():
        challenge.status = "expired"
        raise AuthError("expired", "challenge expired")

    user = await db.get(User, challenge.user_id)
    if user is None or user.disabled:
        raise AuthError("invalid_credentials", "account unavailable")

    challenge.status = "consumed"
    challenge.consumed_at = utcnow()

    session, raw_token = await create_session(db, user, ip, user_agent)
    await write_audit(
        db, actor=_actor_for(user), source="rest", action="auth.login",
        outcome="success", metadata={"challenge_id": challenge.id, "factor": "approval", "ip": ip},
    )
    return user, session, raw_token


async def decide_challenge_by_nonce(
    db: AsyncSession, nonce: str, approve: bool, decided_by: Actor
) -> LoginChallenge:
    """Approve or reject a pending challenge from a Telegram callback nonce.

    The nonce is single-use: any decision consumes it.
    """
    nonce_hash = security.hash_token(nonce)
    result = await db.execute(
        select(LoginChallenge).where(LoginChallenge.approve_nonce_hash == nonce_hash)
    )
    challenge = result.scalar_one_or_none()
    if challenge is None:
        await write_audit(
            db, actor=decided_by, source="telegram", action="auth.challenge.callback",
            outcome="unknown_nonce",
        )
        raise AuthError("invalid_nonce", "unknown or already used approval")

    if challenge.status != "pending":
        await write_audit(
            db, actor=decided_by, source="telegram", action="auth.challenge.callback",
            outcome="replayed", metadata={"challenge_id": challenge.id},
        )
        raise AuthError("replayed", "challenge already decided")

    if (expires_at := _aware(challenge.expires_at)) is None or expires_at < utcnow():
        challenge.status = "expired"
        await write_audit(
            db, actor=decided_by, source="telegram", action="auth.challenge.callback",
            outcome="expired", metadata={"challenge_id": challenge.id},
        )
        raise AuthError("expired", "challenge expired")

    # Consume the nonce regardless of the decision.
    challenge.approve_nonce_hash = ""
    if approve:
        challenge.status = "approved"
        challenge.approved_at = utcnow()
        outcome = "approved"
    else:
        challenge.status = "rejected"
        challenge.rejected_at = utcnow()
        outcome = "rejected"

    await write_audit(
        db, actor=decided_by, source="telegram", action="auth.challenge.decided",
        outcome=outcome, metadata={"challenge_id": challenge.id},
    )
    return challenge


async def recovery_login(
    db: AsyncSession, username: str, password: str, code: str, ip: str, user_agent: str
) -> tuple[User, AuthSession, str]:
    settings = get_settings()
    if not settings.auth_recovery_enabled:
        raise AuthError("recovery_disabled", "recovery is disabled")

    user = await validate_first_factor(db, username, password, ip, user_agent)

    code_hash = security.hash_token(code.strip().lower())
    result = await db.execute(
        select(RecoveryCode).where(
            RecoveryCode.user_id == user.id,
            RecoveryCode.code_hash == code_hash,
            RecoveryCode.used_at.is_(None),
            RecoveryCode.revoked_at.is_(None),
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        await write_audit(
            db, actor=_actor_for(user), source="rest", action="auth.recovery.failed",
            outcome="invalid_code", metadata={"ip": ip},
        )
        raise AuthError("invalid_recovery_code", "invalid recovery code")

    record.used_at = utcnow()
    session, raw_token = await create_session(db, user, ip, user_agent)
    await write_audit(
        db, actor=_actor_for(user), source="rest", action="auth.recovery.used",
        outcome="success", metadata={"ip": ip},
    )
    return user, session, raw_token


async def create_session(
    db: AsyncSession, user: User, ip: str, user_agent: str
) -> tuple[AuthSession, str]:
    settings = get_settings()
    raw_token = security.generate_token()
    session = AuthSession(
        user_id=user.id,
        token_hash=security.hash_token(raw_token),
        csrf_token=security.generate_token(),
        expires_at=utcnow() + timedelta(minutes=settings.session_ttl_minutes),
        ip=ip,
        user_agent=user_agent[:250],
    )
    db.add(session)
    await db.flush()
    await write_audit(
        db, actor=_actor_for(user), source="rest", action="auth.session.created",
        outcome="success", metadata={"session_id": session.id, "ip": ip},
    )
    return session, raw_token


async def resolve_session(db: AsyncSession, raw_token: str) -> tuple[AuthSession, User] | None:
    if not raw_token:
        return None
    result = await db.execute(
        select(AuthSession).where(AuthSession.token_hash == security.hash_token(raw_token))
    )
    session = result.scalar_one_or_none()
    if session is None or session.revoked_at is not None:
        return None
    if (expires_at := _aware(session.expires_at)) is None or expires_at < utcnow():
        return None
    user = await db.get(User, session.user_id)
    if user is None or user.disabled:
        return None
    return session, user


async def revoke_session(db: AsyncSession, session: AuthSession, user: User) -> None:
    session.revoked_at = utcnow()
    await write_audit(
        db, actor=_actor_for(user), source="rest", action="auth.session.revoked",
        outcome="success", metadata={"session_id": session.id},
    )
