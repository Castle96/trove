"""User & token helpers: password hashing, token generation, admin bootstrap.

Password hashing uses stdlib ``hashlib.scrypt`` (memory-hard, no extra deps).
Tokens are random URL-safe strings; only their SHA-256 hash is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import User

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1

VALID_ROLES = ("admin", "operator", "viewer")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _scheme, salt_hex, dk_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 of a bearer token (only the hash is ever persisted)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    return (
        await db.execute(select(User).where(User.username == username.lower().strip()))
    ).scalar_one_or_none()


async def create_user(db: AsyncSession, username: str, password: str, role: str) -> User:
    if role not in VALID_ROLES:
        raise ValueError(f"Role must be one of: {', '.join(VALID_ROLES)}")
    if await get_user_by_username(db, username):
        raise ValueError("A user with that username already exists")
    user = User(
        username=username.lower().strip(),
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


async def bootstrap_admin(db: AsyncSession) -> bool:
    """Create the initial admin from TROVE_ADMIN_USER/_PASSWORD if set."""
    settings = get_settings()
    if not (settings.admin_username and settings.admin_password):
        return False
    count = await db.scalar(select(func.count()).select_from(User))
    if count:
        return False
    await create_user(db, settings.admin_username, settings.admin_password, role="admin")
    await db.commit()
    return True
