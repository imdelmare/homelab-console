"""Cryptographic primitives: password hashing, tokens, OTP and nonce handling."""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.settings import get_settings

_hasher = PasswordHasher()  # Argon2id defaults


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Storage hash for high-entropy tokens (session tokens, nonces)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_otp(length: int | None = None) -> str:
    length = length or get_settings().otp_length
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def otp_hash(challenge_id: str, otp: str) -> str:
    """HMAC the OTP with the server secret, bound to its challenge."""
    key = get_settings().session_secret.encode("utf-8")
    message = f"{challenge_id}:{otp}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def generate_recovery_codes(count: int = 10) -> list[str]:
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(6)
        codes.append(f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}")
    return codes
