import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError

from app.config import get_settings

ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=14)

_hasher = PasswordHasher()  # argon2id by default


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (Argon2Error, InvalidHashError):
        # mismatch, or malformed stored hash — either way: not verified
        return False


def create_access_token(user_id: UUID) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + ACCESS_TTL},
        get_settings().jwt_secret,
        algorithm="HS256",
    )


def decode_access_token(token: str) -> UUID | None:
    """Returns the user id, or None for any invalid/expired token."""
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        return UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def new_refresh_token() -> tuple[str, str, datetime]:
    """Returns (plaintext token, sha256 hash for storage, expiry)."""
    token = secrets.token_urlsafe(48)
    return token, hash_refresh_token(token), datetime.now(UTC) + REFRESH_TTL


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
