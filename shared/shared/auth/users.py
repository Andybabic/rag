"""User accounts for dashboard/app authentication (asyncpg-backed).

Users live in the ``users`` table. Passwords are stored hashed (see
:mod:`shared.auth.passwords`). The first admin is seeded from env at service
startup (see :func:`ensure_bootstrap_admin`); further users are created via the
dashboard.

Roles: ``"admin"`` (may manage users + use cases) and ``"user"`` (chat only).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from shared.auth.passwords import hash_password, verify_password
from shared.db import get_pool

logger = logging.getLogger(__name__)

VALID_ROLES = ("admin", "user")


@dataclass(frozen=True)
class User:
    id: str
    username: str
    role: str
    created_at: str | None = None


def _row_to_user(row: Any) -> User:
    created = row["created_at"]
    return User(
        id=str(row["id"]),
        username=row["username"],
        role=row["role"],
        created_at=created.isoformat() if created is not None else None,
    )


async def _require_pool() -> Any:
    pool = await get_pool()
    if pool is None:
        raise RuntimeError("No DB pool available — cannot access users")
    return pool


async def list_users() -> list[User]:
    """Return all users ordered by username (no password hashes)."""
    pool = await get_pool()
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, username, role, created_at FROM users ORDER BY username ASC"
        )
    return [_row_to_user(r) for r in rows]


async def get_user(username: str) -> User | None:
    pool = await get_pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, role, created_at FROM users WHERE username = $1",
            username,
        )
    return _row_to_user(row) if row else None


async def count_users() -> int:
    pool = await get_pool()
    if pool is None:
        return 0
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")


async def create_user(username: str, password: str, role: str = "user") -> User:
    """Create a user with a freshly hashed password.

    Raises ValueError on invalid role / duplicate username / empty fields.
    """
    username = username.strip()
    if not username:
        raise ValueError("username must not be empty")
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}, got {role!r}")
    if not password:
        raise ValueError("password must not be empty")

    pool = await _require_pool()
    password_hash = hash_password(password)
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO users (username, password_hash, role) "
                "VALUES ($1, $2, $3) "
                "RETURNING id, username, role, created_at",
                username,
                password_hash,
                role,
            )
        except Exception as exc:  # noqa: BLE001 — surface as a clean ValueError
            if "users_username_key" in str(exc) or "duplicate key" in str(exc):
                raise ValueError(f"username {username!r} already exists") from exc
            raise
    return _row_to_user(row)


async def set_password(username: str, password: str) -> bool:
    """Reset a user's password. Returns True if the user existed."""
    if not password:
        raise ValueError("password must not be empty")
    pool = await _require_pool()
    password_hash = hash_password(password)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE users SET password_hash = $2 WHERE username = $1 RETURNING id",
            username,
            password_hash,
        )
    return row is not None


async def set_role(username: str, role: str) -> bool:
    """Change a user's role. Returns True if the user existed."""
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}, got {role!r}")
    pool = await _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE users SET role = $2 WHERE username = $1 RETURNING id",
            username,
            role,
        )
    return row is not None


async def delete_user(username: str) -> bool:
    """Delete a user by username. Returns True if a row was removed."""
    pool = await _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM users WHERE username = $1 RETURNING id",
            username,
        )
    return row is not None


async def verify_user(username: str, password: str) -> User | None:
    """Return the User if credentials are valid, else None.

    Always runs a hash comparison (even for unknown users) to avoid leaking
    account existence via response timing.
    """
    pool = await get_pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, password_hash, role, created_at "
            "FROM users WHERE username = $1",
            username,
        )
    # dummy hash keeps timing roughly constant for unknown usernames
    stored = row["password_hash"] if row else (
        "pbkdf2_sha256$240000$"
        "AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )
    ok = verify_password(password, stored)
    if row and ok:
        return _row_to_user(row)
    return None


async def ensure_bootstrap_admin(username: str, password: str) -> bool:
    """Create the initial admin from env if the users table is empty.

    Returns True if an admin was created, False if users already existed or the
    credentials were not provided. Safe to call on every startup.
    """
    if not username or not password:
        logger.info("Bootstrap admin skipped: ADMIN_USERNAME/ADMIN_PASSWORD unset")
        return False
    pool = await get_pool()
    if pool is None:
        logger.warning("Bootstrap admin skipped: no DB pool available")
        return False
    if await count_users() > 0:
        return False
    await create_user(username, password, role="admin")
    logger.info("Bootstrap admin %r created from env", username)
    return True
