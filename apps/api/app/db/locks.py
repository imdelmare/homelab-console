import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def advisory_lock_key(name: str) -> int:
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


async def try_advisory_xact_lock(db: AsyncSession, name: str) -> bool:
    if not db.bind or db.bind.dialect.name != "postgresql":
        return True
    result = await db.execute(
        text("select pg_try_advisory_xact_lock(:key)"),
        {"key": advisory_lock_key(name)},
    )
    return bool(result.scalar_one())
