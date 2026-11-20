from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    locked: bool
    username: str


def hash_password(hasher: PasswordHasher, password: str) -> str:
    return hasher.hash(password)


def verify_password(hasher: PasswordHasher, password_hash: str, password: str) -> bool:
    try:
        return bool(hasher.verify(password_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_password_hash(hasher: PasswordHasher) -> str:
    return hasher.hash("skylight-wifi-dummy-password")


async def attempt_login(
    store: Any,
    hasher: PasswordHasher,
    dummy_hash: str,
    username: str,
    password: str,
    ip: str,
    now: datetime,
) -> LoginResult:
    """Check a password. The password is never returned or logged."""
    safe_name = (username or "").strip()[:64] or "unknown"
    safe_ip = (ip or "unknown")[:64]
    if await store.login_is_locked(safe_name, safe_ip, now):
        verify_password(hasher, dummy_hash, password or "x")
        return LoginResult(False, True, safe_name)
    admin = await store.get_admin()
    if admin is not None and admin.username == safe_name and password:
        ok = verify_password(hasher, admin.password_hash, password)
        canonical = admin.username
    else:
        verify_password(hasher, dummy_hash, password or "x")
        ok = False
        canonical = safe_name
    if ok:
        await store.clear_failures(canonical, safe_ip)
        return LoginResult(True, False, canonical)
    locked = await store.record_failure(safe_name, safe_ip, now)
    return LoginResult(False, locked, safe_name)
