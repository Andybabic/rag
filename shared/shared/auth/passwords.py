"""Password hashing — PBKDF2-HMAC-SHA256, stdlib only (no extra dependency).

Stored format (single string, like passlib's MCF)::

    pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>

``verify_password`` is constant-time. Iteration count is embedded in the hash
so it can be raised later without breaking existing hashes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 240_000
_SALT_BYTES = 16


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    """Hash a plaintext password into a self-describing storage string."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, stored: str) -> bool:
    """Return True if *password* matches the *stored* hash. Constant-time."""
    if not password or not stored:
        return False
    try:
        algorithm, iter_str, salt_b64, hash_b64 = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iter_str)
        salt = _unb64(salt_b64)
        expected = _unb64(hash_b64)
    except (ValueError, TypeError):
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(derived, expected)
