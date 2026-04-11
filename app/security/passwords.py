"""Argon2id password hashing helpers.

Wrapping `argon2-cffi` in a tiny module keeps the rest of the codebase
blissfully unaware of the specific algorithm and its parameters — only
this file knows about `PasswordHasher`.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# A single hasher instance is fine; all methods are thread-safe.
# Parameters default to the OWASP-recommended profile:
#   time_cost=3, memory_cost=64 MiB, parallelism=4.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return an Argon2id hash for the given plaintext password."""
    if not password:
        raise ValueError("password must not be empty")
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Constant-time verification of a plaintext password against a hash.

    Returns False (never raises) if the password is wrong, the hash is
    malformed, or the user has no password set (stored_hash is None).
    """
    if not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True if the hash should be rewritten with current parameters.

    Call this after a successful login and, if it returns True, re-hash
    the password and persist it. This keeps hashes fresh as the Argon2
    profile evolves.
    """
    return _hasher.check_needs_rehash(stored_hash)
