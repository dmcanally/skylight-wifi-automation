from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
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


async def _limited_to_thread(func: Callable[..., Any], *args: Any) -> Any:
    """Run func off the loop and hold the slot until that thread finishes.

    Cancelling the caller used to leave the semaphore inside ``async with``
    and free the slot while argon2 was still running. The done callback
    releases the slot, and shield keeps the worker alive across that cancel.
    """
    limit = _hasher_limit()
    await limit.acquire()
    try:
        task = asyncio.ensure_future(asyncio.to_thread(func, *args))
    except BaseException:
        limit.release()
        raise
    task.add_done_callback(lambda _done: limit.release())
    return await asyncio.shield(task)


def argon2_concurrency() -> int:
    raw = os.environ.get("ARGON2_CONCURRENCY", "2")
    try:
        value = int(raw)
    except ValueError:
        return 2
    return min(32, max(1, value))


def _hasher_limit() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limit = getattr(loop, "_swa_argon2_limit", None)
    if limit is None:
        limit = asyncio.Semaphore(argon2_concurrency())
        loop._swa_argon2_limit = limit  # type: ignore[attr-defined]
    return limit


async def hash_password_off_loop(hasher: PasswordHasher, password: str) -> str:
    return await _limited_to_thread(hash_password, hasher, password)


async def verify_password_off_loop(
    hasher: PasswordHasher, password_hash: str, password: str
) -> bool:
    return await _limited_to_thread(verify_password, hasher, password_hash, password)


async def attempt_login(
    store: Any,
    hasher: PasswordHasher,
    dummy_hash: str,
    username: str,
    password: str,
    ip: str,
    now: datetime,
) -> LoginResult:
    """Check a password. Hashing runs off the poller loop. The password is never logged."""
    safe_name = (username or "").strip()[:64] or "unknown"
    safe_ip = (ip or "unknown")[:64]
    if await store.login_is_locked(safe_name, safe_ip, now):
        await verify_password_off_loop(hasher, dummy_hash, password or "x")
        return LoginResult(False, True, safe_name)
    admin = await store.get_admin()
    if admin is not None and admin.username == safe_name and password:
        ok = await verify_password_off_loop(hasher, admin.password_hash, password)
        canonical = admin.username
    else:
        await verify_password_off_loop(hasher, dummy_hash, password or "x")
        ok = False
        canonical = safe_name
    if ok:
        await store.clear_failures(canonical, safe_ip)
        return LoginResult(True, False, canonical)
    locked = await store.record_failure(safe_name, safe_ip, now)
    return LoginResult(False, locked, safe_name)
