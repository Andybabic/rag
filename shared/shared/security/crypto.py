"""Symmetric encryption for at-rest secrets (e.g. API keys in DB).

Uses Fernet (AES-128-CBC + HMAC-SHA256). The key comes from the env var
``CONFIG_MASTER_KEY`` and is a URL-safe base64 string of 32 bytes. Generate
one with::

    python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'

If the env var is unset, encryption is unavailable: ``encrypt()`` raises
``CryptoError`` and ``decrypt()`` likewise. Reading/writing usecase config
without a master key is allowed for non-secret fields; only API-key fields
need encryption.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(RuntimeError):
    """Raised when encryption is requested but unavailable / fails."""


_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    key = os.getenv("CONFIG_MASTER_KEY", "").strip()
    if not key:
        raise CryptoError(
            "CONFIG_MASTER_KEY is not set. Generate one via "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'` and put it in .env."
        )
    try:
        _fernet = Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"CONFIG_MASTER_KEY is not a valid Fernet key: {exc}") from exc
    return _fernet


def is_configured() -> bool:
    """True if a master key is set and parseable."""
    try:
        _get_fernet()
        return True
    except CryptoError:
        return False


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns URL-safe base64 ciphertext."""
    if plaintext == "":
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt URL-safe base64 ciphertext back to a string."""
    if ciphertext == "":
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise CryptoError(
            "Failed to decrypt — the stored value was encrypted with a "
            "different master key, or the data is corrupt."
        ) from exc
