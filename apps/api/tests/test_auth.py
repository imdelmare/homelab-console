from datetime import timedelta

from sqlalchemy import select

from app.db.models import LoginChallenge, utcnow
from app.db.session import get_session_factory
from app.domain.actors import Actor
from tests.conftest import do_login

TELEGRAM_OPERATOR = Actor(kind="telegram", id="111", label="test")


async def _expire_challenge(challenge_id: str) -> None:
    async with get_session_factory()() as db:
        challenge = await db.get(LoginChallenge, challenge_id)
        assert challenge is not None
        challenge.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()


async def test_login_creates_challenge(client, user, capture_adapter):
    response = await client.post(
        "/api/auth/login", json={"username": "operator", "password": "correct-horse-battery"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["challenge_id"] == capture_adapter.challenge_id
    assert capture_adapter.otp  # OTP was generated and delivered
    status = await client.get(f"/api/auth/challenge/{body['challenge_id']}")
    assert status.json()["status"] == "pending"


async def test_wrong_password_rejected(client, user, capture_adapter):
    response = await client.post(
        "/api/auth/login", json={"username": "operator", "password": "wrong"}
    )
    assert response.status_code == 401


async def test_telegram_only_login_requires_only_username(
    client, user, capture_adapter, monkeypatch
):
    from app.core.settings import get_settings

    monkeypatch.setenv("AUTH_LOGIN_MODE", "telegram_only")
    get_settings.cache_clear()

    response = await client.post(
        "/api/auth/login", json={"username": "operator"}
    )

    assert response.status_code == 200
    assert response.json()["challenge_id"] == capture_adapter.challenge_id
    assert capture_adapter.otp
    verified = await client.post(
        "/api/auth/verify-otp",
        json={
            "challenge_id": response.json()["challenge_id"],
            "otp": capture_adapter.otp,
        },
    )
    assert verified.status_code == 200
    assert verified.json()["authenticated"] is True


async def test_telegram_only_login_rejects_password(
    client, user, monkeypatch
):
    from app.core.settings import get_settings

    monkeypatch.setenv("AUTH_LOGIN_MODE", "telegram_only")
    get_settings.cache_clear()

    response = await client.post(
        "/api/auth/login",
        json={"username": "operator", "password": "correct-horse-battery"},
    )

    assert response.status_code == 400


async def test_telegram_only_login_rejects_unknown_user(client, monkeypatch):
    from app.core.settings import get_settings

    monkeypatch.setenv("AUTH_LOGIN_MODE", "telegram_only")
    get_settings.cache_clear()

    response = await client.post(
        "/api/auth/login", json={"username": "unknown"}
    )

    assert response.status_code == 401


async def test_telegram_only_login_rejects_disabled_user(
    client, user, db_session, monkeypatch
):
    from app.core.settings import get_settings

    user.disabled = True
    await db_session.commit()
    monkeypatch.setenv("AUTH_LOGIN_MODE", "telegram_only")
    get_settings.cache_clear()

    response = await client.post(
        "/api/auth/login", json={"username": "operator"}
    )

    assert response.status_code == 401


async def test_telegram_only_login_attempts_are_rate_limited(client, monkeypatch):
    from app.core.settings import get_settings

    monkeypatch.setenv("AUTH_LOGIN_MODE", "telegram_only")
    get_settings.cache_clear()

    responses = [
        await client.post("/api/auth/login", json={"username": "unknown"})
        for _ in range(6)
    ]

    assert [response.status_code for response in responses[:5]] == [401] * 5
    assert responses[5].status_code == 429


async def test_telegram_only_config_disables_password_recovery(
    client, user, monkeypatch
):
    from app.core.settings import get_settings

    monkeypatch.setenv("AUTH_LOGIN_MODE", "telegram_only")
    get_settings.cache_clear()

    config = await client.get("/api/auth/config")
    recovery = await client.post(
        "/api/auth/recovery",
        json={
            "username": "operator",
            "password": "correct-horse-battery",
            "recovery_code": user.recovery_codes[0],
        },
    )

    assert config.status_code == 200
    assert config.headers["cache-control"] == "no-store"
    assert config.json()["login_mode"] == "telegram_only"
    assert config.json()["methods"]["recovery"] is False
    assert recovery.status_code == 404


async def test_otp_login_and_protected_access(client, user, capture_adapter):
    body, csrf = await do_login(client, capture_adapter)
    assert body["authenticated"] is True

    tools = await client.get("/api/tools")
    assert tools.status_code == 200

    session_info = await client.get("/api/auth/session")
    assert session_info.json()["authenticated"] is True


async def test_protected_endpoints_require_auth(client):
    for path in ("/api/tools", "/api/tasks", "/api/providers", "/api/audit",
                 "/api/inventory/hosts"):
        response = await client.get(path)
        assert response.status_code == 401, path
    run = await client.post("/api/tools/proxmox.version/run", json={"input": {}})
    assert run.status_code == 401


async def test_health_is_public(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_is_public_and_checks_database(client):
    response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


async def test_invalid_otp_rejected(client, user, capture_adapter):
    response = await client.post(
        "/api/auth/login", json={"username": "operator", "password": "correct-horse-battery"}
    )
    challenge_id = response.json()["challenge_id"]
    verify = await client.post(
        "/api/auth/verify-otp", json={"challenge_id": challenge_id, "otp": "00000000"}
    )
    assert verify.status_code == 401


async def test_otp_max_attempts(client, user, capture_adapter):
    response = await client.post(
        "/api/auth/login", json={"username": "operator", "password": "correct-horse-battery"}
    )
    challenge_id = response.json()["challenge_id"]
    for _ in range(3):  # LOGIN_CHALLENGE_MAX_ATTEMPTS=3 in tests
        await client.post(
            "/api/auth/verify-otp", json={"challenge_id": challenge_id, "otp": "00000000"}
        )
    # Correct OTP no longer works: the challenge burned its attempts.
    verify = await client.post(
        "/api/auth/verify-otp", json={"challenge_id": challenge_id, "otp": capture_adapter.otp}
    )
    assert verify.status_code in (401, 410)


async def test_otp_single_use(client, user, capture_adapter):
    response = await client.post(
        "/api/auth/login", json={"username": "operator", "password": "correct-horse-battery"}
    )
    challenge_id = response.json()["challenge_id"]
    first = await client.post(
        "/api/auth/verify-otp", json={"challenge_id": challenge_id, "otp": capture_adapter.otp}
    )
    assert first.status_code == 200
    replay = await client.post(
        "/api/auth/verify-otp", json={"challenge_id": challenge_id, "otp": capture_adapter.otp}
    )
    assert replay.status_code == 401


async def test_otp_expiration(client, user, capture_adapter):
    response = await client.post(
        "/api/auth/login", json={"username": "operator", "password": "correct-horse-battery"}
    )
    challenge_id = response.json()["challenge_id"]
    await _expire_challenge(challenge_id)

    status = await client.get(f"/api/auth/challenge/{challenge_id}")
    assert status.json()["status"] == "expired"

    verify = await client.post(
        "/api/auth/verify-otp", json={"challenge_id": challenge_id, "otp": capture_adapter.otp}
    )
    assert verify.status_code == 410


async def test_new_challenge_invalidates_previous(client, user, capture_adapter):
    first = await client.post(
        "/api/auth/login", json={"username": "operator", "password": "correct-horse-battery"}
    )
    first_id = first.json()["challenge_id"]
    first_otp = capture_adapter.otp

    await client.post(
        "/api/auth/login", json={"username": "operator", "password": "correct-horse-battery"}
    )
    verify = await client.post(
        "/api/auth/verify-otp", json={"challenge_id": first_id, "otp": first_otp}
    )
    assert verify.status_code == 401


async def test_telegram_approval_flow(client, user, capture_adapter):
    from app.services.auth_service import decide_challenge_by_nonce

    response = await client.post(
        "/api/auth/login", json={"username": "operator", "password": "correct-horse-battery"}
    )
    challenge_id = response.json()["challenge_id"]

    # Completing before approval must fail.
    early = await client.post("/api/auth/complete", json={"challenge_id": challenge_id})
    assert early.status_code == 401

    async with get_session_factory()() as db:
        await decide_challenge_by_nonce(db, capture_adapter.nonce, True, TELEGRAM_OPERATOR)
        await db.commit()

    status = await client.get(f"/api/auth/challenge/{challenge_id}")
    assert status.json()["status"] == "approved"

    complete = await client.post("/api/auth/complete", json={"challenge_id": challenge_id})
    assert complete.status_code == 200
    assert complete.json()["authenticated"] is True

    # The challenge is consumed: completing again must fail.
    again = await client.post("/api/auth/complete", json={"challenge_id": challenge_id})
    assert again.status_code == 401


async def test_approval_nonce_single_use(client, user, capture_adapter, db_session):
    import pytest

    from app.services.auth_service import AuthError, decide_challenge_by_nonce

    await client.post(
        "/api/auth/login", json={"username": "operator", "password": "correct-horse-battery"}
    )
    nonce = capture_adapter.nonce
    await decide_challenge_by_nonce(db_session, nonce, False, TELEGRAM_OPERATOR)
    await db_session.commit()

    with pytest.raises(AuthError):
        await decide_challenge_by_nonce(db_session, nonce, True, TELEGRAM_OPERATOR)


async def test_login_rate_limited(client, user, capture_adapter):
    responses = []
    for _ in range(8):
        response = await client.post(
            "/api/auth/login", json={"username": "operator", "password": "wrong"}
        )
        responses.append(response.status_code)
    assert 429 in responses


async def test_logout_revokes_session(client, user, capture_adapter):
    _, csrf = await do_login(client, capture_adapter)
    logout = await client.post("/api/auth/logout", headers={"x-csrf-token": csrf})
    assert logout.status_code == 204
    after = await client.get("/api/tools")
    assert after.status_code == 401


async def test_csrf_required_for_state_changes(client, user, capture_adapter):
    await do_login(client, capture_adapter)
    # No CSRF header on a state-changing request.
    response = await client.post(
        "/api/tasks", json={"title": "x", "goal": ""}
    )
    assert response.status_code == 403


async def test_recovery_code_login(client, user, capture_adapter):
    code = user.recovery_codes[0]
    response = await client.post(
        "/api/auth/recovery",
        json={"username": "operator", "password": "correct-horse-battery", "recovery_code": code},
    )
    assert response.status_code == 200
    # Single use.
    replay = await client.post(
        "/api/auth/recovery",
        json={"username": "operator", "password": "correct-horse-battery", "recovery_code": code},
    )
    assert replay.status_code == 401
