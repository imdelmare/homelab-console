from sqlalchemy import select

from app.core import security
from app.core.settings import get_settings
from app.db.models import AuditEvent, RecoveryCode, User
from app import cli
from app.services.admin_bootstrap import create_first_admin, validate_bootstrap_input


def test_bootstrap_input_is_bounded():
    assert validate_bootstrap_input("operator", "correct-horse-battery") == (
        "operator",
        "correct-horse-battery",
    )
    for username, password in (
        ("", "correct-horse-battery"),
        ("bad user", "correct-horse-battery"),
        ("operator", "short"),
    ):
        try:
            validate_bootstrap_input(username, password)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid bootstrap input was accepted")


def test_cli_refuses_noninteractive_password_input(monkeypatch, capsys):
    monkeypatch.setattr(get_settings(), "app_env", "live")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "app.cli.getpass.getpass",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("password prompt must not run")),
    )

    assert cli._prompt_create_admin() == 2
    assert "requires an interactive terminal" in capsys.readouterr().err


async def test_first_admin_is_created_with_recovery_hashes_and_audit(db_session):
    password = "correct-horse-battery"
    result = await create_first_admin(
        db_session,
        username="operator",
        password=password,
    )
    await db_session.commit()

    assert result.created is True
    assert result.code == "created"
    assert len(result.recovery_codes) == 10
    user = await db_session.scalar(select(User).where(User.username == "operator"))
    assert user is not None
    assert security.verify_password(user.password_hash, password)
    recovery_rows = (
        await db_session.execute(select(RecoveryCode).where(RecoveryCode.user_id == user.id))
    ).scalars().all()
    assert len(recovery_rows) == 10
    assert not any(code in {row.code_hash for row in recovery_rows} for code in result.recovery_codes)

    event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "auth.admin.bootstrap")
    )
    assert event is not None
    assert event.outcome == "success"
    assert event.meta == {"username": "operator"}
    assert password not in str(event.meta)
    assert not any(code in str(event.meta) for code in result.recovery_codes)


async def test_bootstrap_refuses_a_second_account_and_audits_attempt(db_session):
    first = await create_first_admin(
        db_session,
        username="operator",
        password="correct-horse-battery",
    )
    assert first.created is True
    await db_session.commit()

    second = await create_first_admin(
        db_session,
        username="other",
        password="another-correct-password",
    )
    await db_session.commit()

    assert second.created is False
    assert second.code == "already_initialized"
    assert await db_session.scalar(select(User).where(User.username == "other")) is None
    events = (
        await db_session.execute(
            select(AuditEvent)
            .where(AuditEvent.action == "auth.admin.bootstrap")
            .order_by(AuditEvent.created_at)
        )
    ).scalars().all()
    assert [event.outcome for event in events] == ["success", "already_initialized"]
