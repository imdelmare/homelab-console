"""Restricted operator CLI for installation lifecycle actions."""

import argparse
import asyncio
import getpass
import sys

from app.core.settings import get_settings
from app.db.session import get_session_factory, init_db
from app.services.admin_bootstrap import create_first_admin, validate_bootstrap_input


async def _create_admin(username: str, password: str) -> int:
    await init_db()
    async with get_session_factory()() as db:
        result = await create_first_admin(db, username=username, password=password)
        await db.commit()

    if result.created:
        print(f"Administrator created: {result.username}")
        print("Recovery codes — store them securely; they will not be shown again:")
        for code in result.recovery_codes:
            print(code)
        return 0
    if result.code == "already_initialized":
        print("Refusing bootstrap: an account already exists.", file=sys.stderr)
        return 3
    print("Refusing bootstrap: another bootstrap is in progress.", file=sys.stderr)
    return 4


def _prompt_create_admin() -> int:
    if not get_settings().is_live:
        print("create-admin is available only with APP_ENV=live.", file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print("create-admin requires an interactive terminal.", file=sys.stderr)
        return 2
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return 2
    try:
        validate_bootstrap_input(username, password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        return asyncio.run(_create_admin(username, password))
    except Exception as exc:
        print(
            f"Administrator bootstrap failed ({exc.__class__.__name__}). "
            "Verify database availability and migrations.",
            file=sys.stderr,
        )
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=("create-admin",))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "create-admin":
        return _prompt_create_admin()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
