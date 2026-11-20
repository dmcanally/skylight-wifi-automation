from __future__ import annotations

import hashlib
import hmac
import secrets


def new_token() -> str:
    """32 random bytes, URL-safe. Store only a hash of session cookies."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def constant_equal(left: str, right: str) -> bool:
    left_b = left.encode()
    right_b = right.encode()
    if len(left_b) != len(right_b):
        hmac.compare_digest(hashlib.sha256(left_b).digest(), hashlib.sha256(right_b).digest())
        return False
    return hmac.compare_digest(left_b, right_b)
