"""Bearer tokens for the public pipeline API.

A token is a random 32-byte secret rendered as ``kmt_<43 base64url chars>``.
Only its SHA-256 hash reaches the database, so a database dump does not hand
anyone a working credential and a lost token can only be replaced, never
recovered — the same trade-off every "personal access token" makes.

SHA-256 rather than PBKDF2 (which :mod:`shared.auth.passwords` uses for
passwords) is deliberate: the secret is 256 bits of entropy from
``secrets.token_urlsafe``, so there is nothing to brute-force, and the hash is
looked up on *every* API call — a 240k-iteration KDF would add real latency to
each request for no gain.

Scope: ``use_cases`` restricts a token to specific use cases. An empty list
means "all use cases", mirroring how admin accounts relate to
``user_use_cases``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from core.database import get_pool

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "kmt_"
_PREFIX_DISPLAY_CHARS = 12


@dataclass(frozen=True)
class ApiToken:
    id: str
    name: str
    token_prefix: str
    use_cases: list[str]
    created_by: str | None
    created_at: str | None
    last_used_at: str | None
    expires_at: str | None
    revoked_at: str | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None and not self._expired

    @property
    def _expired(self) -> bool:
        if not self.expires_at:
            return False
        return datetime.fromisoformat(self.expires_at) <= datetime.now(timezone.utc)

    def allows(self, use_case: str) -> bool:
        """Empty scope = every use case; otherwise the id must be listed."""
        return not self.use_cases or use_case in self.use_cases


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _row_to_token(row: Any) -> ApiToken:
    raw_scope = row["use_cases"]
    # asyncpg hands JSONB back as text unless a codec is registered.
    scope = json.loads(raw_scope) if isinstance(raw_scope, str) else (raw_scope or [])
    return ApiToken(
        id=str(row["id"]),
        name=row["name"],
        token_prefix=row["token_prefix"],
        use_cases=list(scope),
        created_by=row["created_by"],
        created_at=_iso(row["created_at"]),
        last_used_at=_iso(row["last_used_at"]),
        expires_at=_iso(row["expires_at"]),
        revoked_at=_iso(row["revoked_at"]),
    )


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


async def create_token(
    name: str,
    *,
    use_cases: list[str] | None = None,
    created_by: str | None = None,
    expires_in_days: int | None = None,
) -> tuple[ApiToken, str]:
    """Create a token. Returns (record, plaintext) — plaintext only here."""
    if not name.strip():
        raise ValueError("name must not be empty")

    plaintext = generate_token()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=expires_in_days)
        if expires_in_days
        else None
    )

    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO api_tokens (name, token_prefix, token_hash, use_cases,
                                   created_by, expires_at)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6)
           RETURNING id, name, token_prefix, use_cases, created_by, created_at,
                     last_used_at, expires_at, revoked_at""",
        name.strip(),
        plaintext[:_PREFIX_DISPLAY_CHARS],
        hash_token(plaintext),
        json.dumps(sorted(set(use_cases or []))),
        created_by,
        expires_at,
    )
    return _row_to_token(row), plaintext


async def list_tokens() -> list[ApiToken]:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, name, token_prefix, use_cases, created_by, created_at,
                  last_used_at, expires_at, revoked_at
           FROM api_tokens ORDER BY created_at DESC"""
    )
    return [_row_to_token(r) for r in rows]


async def revoke_token(token_id: str) -> bool:
    """Mark a token as revoked. Idempotent — re-revoking keeps the first time."""
    pool = await get_pool()
    result = await pool.execute(
        """UPDATE api_tokens SET revoked_at = NOW()
           WHERE id = $1::uuid AND revoked_at IS NULL""",
        token_id,
    )
    return result.endswith(" 1")


async def delete_token(token_id: str) -> bool:
    pool = await get_pool()
    result = await pool.execute("DELETE FROM api_tokens WHERE id = $1::uuid", token_id)
    return result.endswith(" 1")


async def verify_token(plaintext: str) -> ApiToken | None:
    """Resolve a bearer secret into its token record.

    Returns ``None`` for unknown, revoked and expired tokens alike — the caller
    must not tell an attacker which of the three it was. ``last_used_at`` is
    refreshed on success so the dashboard can show dormant credentials.
    """
    if not plaintext:
        return None
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT id, name, token_prefix, use_cases, created_by, created_at,
                  last_used_at, expires_at, revoked_at
           FROM api_tokens WHERE token_hash = $1""",
        hash_token(plaintext),
    )
    if row is None:
        return None

    token = _row_to_token(row)
    if not token.active:
        return None

    try:
        await pool.execute(
            "UPDATE api_tokens SET last_used_at = NOW() WHERE id = $1::uuid", token.id
        )
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never fail a call
        logger.warning("Could not update last_used_at for token %s: %s", token.id, exc)

    return token
