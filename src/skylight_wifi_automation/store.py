from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig
from .evaluator import past_cutoff
from .overrides import (
    UNBLOCK_CONFIRMATION,
    ActiveOverride,
    ConfirmationRequired,
    OverrideError,
    override_expiry,
)
from .redact import REDACTOR
from .secrets import SECRET_NAMES, ResolvedSecrets, decrypt, encrypt
from .security import constant_equal, hash_token, new_token
from .timeutil import dump_dt, load_dt

LOGGER = logging.getLogger(__name__)

# Login subjects are truncated to 64 characters. This key is longer, and it
# starts with a byte the username form rejects, so a setup lockout cannot
# collide with an account named "setup".
SETUP_LOCK_SUBJECT = "\x00setup-lockout-" + ("0" * 64)

SETTING_KEYS = (
    "timezone",
    "poll_seconds",
    "dry_run",
    "include_late",
    "include_routines",
    "empty_day_unlocks",
    "unifi_url",
    "unifi_site",
    "unifi_tls_ca",
)
IMPORT_KEY = "imported_yaml_sha256"
LOCK_WINDOW = timedelta(minutes=15)
IDLE_TIMEOUT = timedelta(hours=12)
SHORT_SESSION = timedelta(hours=12)
REMEMBER_SESSION = timedelta(days=30)
TOUCH_AFTER = timedelta(minutes=15)
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_at TEXT NOT NULL,
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    csrf TEXT NOT NULL,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    remember INTEGER NOT NULL DEFAULT 0,
    user_agent TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY,
    ip TEXT NOT NULL,
    username TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    lock_kind TEXT CHECK (lock_kind IS NULL OR lock_kind IN ('user', 'ip', 'both')),
    locked_until TEXT
);
CREATE INDEX IF NOT EXISTS login_attempts_ip ON login_attempts(ip, attempted_at);
CREATE INDEX IF NOT EXISTS login_attempts_user ON login_attempts(username, attempted_at);
CREATE TABLE IF NOT EXISTS form_tokens (
    token_hash TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS children (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL COLLATE NOCASE UNIQUE,
    skylight_profile TEXT NOT NULL COLLATE NOCASE UNIQUE,
    object_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    cutoff TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS overrides (
    id INTEGER PRIMARY KEY,
    child_id INTEGER NOT NULL REFERENCES children(id),
    action TEXT NOT NULL CHECK (action IN ('unblock', 'block')),
    expires_at TEXT NOT NULL,
    set_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    cleared_at TEXT,
    cleared_by TEXT,
    clear_reason TEXT,
    CHECK (
        (cleared_at IS NULL AND clear_reason IS NULL)
        OR (cleared_at IS NOT NULL AND clear_reason IN ('manual', 'expired', 'replaced'))
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS overrides_one_active
    ON overrides(child_id) WHERE cleared_at IS NULL;
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    diff TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_id ON audit(id);
CREATE TABLE IF NOT EXISTS config_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT INTO config_version(id, version)
SELECT 1, 0 WHERE NOT EXISTS (SELECT 1 FROM config_version WHERE id = 1);
"""


class LastAdminError(ValueError):
    """Refusing a change that would leave no active admin."""


class ConfigWriteError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__(errors[0] if errors else "invalid configuration")
        self.errors = errors


@dataclass(frozen=True)
class Admin:
    username: str
    password_hash: str
    failed_count: int
    locked_until: datetime | None
    role: str = "admin"
    disabled: bool = False

    def __repr__(self) -> str:
        return f"Admin(username={self.username!r}, role={self.role!r}, failed_count={self.failed_count})"


@dataclass(frozen=True)
class SessionInfo:
    id: int
    username: str
    created_at: datetime
    last_seen: datetime
    expires_at: datetime
    user_agent: str
    current: bool
    remember: bool


@dataclass(frozen=True)
class Session:
    token_hash: str
    csrf: str
    username: str
    created_at: datetime
    expires_at: datetime
    last_seen: datetime
    role: str = "admin"

    def __repr__(self) -> str:
        return f"Session(username={self.username!r})"


@dataclass(frozen=True)
class ChildRecord:
    id: int
    label: str
    skylight_profile: str
    object_name: str
    cutoff: str


@dataclass(frozen=True)
class SecretStatus:
    name: str
    state: str
    source: str
    updated_at: str | None
    note: str


@dataclass(frozen=True)
class AuditRow:
    ts: str
    actor: str
    action: str
    target: str
    diff: str


@dataclass(frozen=True)
class AuditPage:
    rows: list[AuditRow]
    page: int
    pages: int
    total: int


def _require_writable_dir(directory: Path) -> None:
    if not directory.is_dir():
        raise RuntimeError(f"DATA_DIR is not writable: {directory}")
    probe = directory / f".swa-write-probe-{os.getpid()}-{threading.get_ident()}"
    try:
        with probe.open("xb") as handle:
            handle.write(b"ok")
            handle.flush()
    except OSError:
        raise RuntimeError(f"DATA_DIR is not writable: {directory}") from None
    try:
        probe.unlink()
    except OSError:
        raise RuntimeError(f"DATA_DIR is not writable: {directory}") from None
    _restrict_dir_mode(directory)


def _restrict_dir_mode(directory: Path) -> None:
    try:
        owned = directory.stat().st_uid == os.getuid()
    except OSError:
        LOGGER.warning("could not set DATA_DIR mode to 0700: %s", directory)
        return
    if not owned:
        LOGGER.warning(
            "DATA_DIR is not owned by this process; leaving its mode unchanged: %s",
            directory,
        )
        return
    try:
        os.chmod(directory, 0o700)
    except OSError:
        LOGGER.warning("could not set DATA_DIR mode to 0700: %s", directory)


def _private_mode(path: Path) -> None:
    if path.exists():
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


class Store:
    """SQLite in WAL mode. Public methods run the work via asyncio.to_thread."""

    def __init__(self, path: Path, key: bytes | None) -> None:
        self.path = path
        self._key = key
        self.encryption_enabled = key is not None
        _require_writable_dir(path.parent)
        previous = os.umask(0o077)
        try:
            try:
                self._conn = sqlite3.connect(
                    path, check_same_thread=False, isolation_level=None, timeout=5
                )
            except sqlite3.OperationalError:
                raise RuntimeError(f"DATA_DIR is not writable: {path.parent}") from None
        finally:
            os.umask(previous)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._migrate()
        self._restrict_files()

    def __repr__(self) -> str:
        return f"Store(path={str(self.path)!r})"

    def _restrict_files(self) -> None:
        _private_mode(self.path)
        for suffix in ("-wal", "-shm"):
            _private_mode(Path(f"{self.path}{suffix}"))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._restrict_files()
            current = 0
            exists = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
            ).fetchone()
            if exists is not None:
                row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
                current = int(row[0] or 0)
            if current < 1:
                self._conn.executescript(SCHEMA)
                self._conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
                return
            if current < SCHEMA_VERSION:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._migrate_v2(self._conn)
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

    def _column_names(self, conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        """Copy the single admin account into users. Safe to run once per database."""
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
                disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
                created_at TEXT NOT NULL,
                failed_count INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT
            )"""
        )
        admin_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'admin'"
        ).fetchone()
        if admin_exists is not None:
            conn.execute(
                """INSERT OR IGNORE INTO users(
                       username, password_hash, role, disabled, created_at, failed_count, locked_until
                   )
                   SELECT username, password_hash, 'admin', 0, ?, failed_count, locked_until FROM admin""",
                (dump_dt(datetime.now(UTC)),),
            )
            conn.execute("DROP TABLE admin")
        columns = self._column_names(conn, "sessions")
        if "remember" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN remember INTEGER NOT NULL DEFAULT 0")
        if "user_agent" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN user_agent TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT INTO schema_version(version) VALUES (2)")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _put(self, conn: sqlite3.Connection, key: str, value: object) -> None:
        conn.execute(
            """INSERT INTO settings(key, value) VALUES(?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, json.dumps(value)),
        )

    def _setting(self, conn: sqlite3.Connection, key: str, default: object = None) -> object:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def _settings_map(self, conn: sqlite3.Connection) -> dict[str, object]:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def _audit(
        self,
        conn: sqlite3.Connection,
        actor: str,
        action: str,
        target: str,
        diff: dict[str, object],
        now: datetime,
    ) -> None:
        payload = REDACTOR.scrub_text(json.dumps(diff, sort_keys=True, default=str))
        if len(payload) > 2000:
            payload = payload[:2000]
        conn.execute(
            "INSERT INTO audit(ts, actor, action, target, diff) VALUES (?, ?, ?, ?, ?)",
            (dump_dt(now), str(actor)[:64], str(action)[:64], str(target)[:200], payload),
        )

    def _bump(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE config_version SET version = version + 1 WHERE id = 1")

    def _journal_mode(self) -> str:
        with self._lock:
            row = self._conn.execute("PRAGMA journal_mode").fetchone()
            return str(row[0])

    def _config_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT version FROM config_version WHERE id = 1").fetchone()
            return int(row[0])

    def _user_from(self, row: sqlite3.Row | None) -> Admin | None:
        if row is None:
            return None
        locked = load_dt(row["locked_until"]) if row["locked_until"] else None
        return Admin(
            row["username"],
            row["password_hash"],
            int(row["failed_count"]),
            locked,
            str(row["role"]),
            bool(row["disabled"]),
        )

    def _get_user(self, username: str) -> Admin | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            return self._user_from(row)

    def _get_admin(self) -> Admin | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM users WHERE role = 'admin'
                   ORDER BY disabled, created_at LIMIT 1"""
            ).fetchone()
            return self._user_from(row)

    def _list_users(self) -> list[Admin]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM users ORDER BY role, username").fetchall()
        found: list[Admin] = []
        for row in rows:
            user = self._user_from(row)
            if user is not None:
                found.append(user)
        return found

    def _active_admin_count(self, conn: sqlite3.Connection) -> int:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0"
            ).fetchone()[0]
        )

    def _guard_last_admin(
        self,
        conn: sqlite3.Connection,
        username: str,
        *,
        delete: bool = False,
        disable: bool = False,
        role: str | None = None,
    ) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            raise KeyError(username)
        active = row["role"] == "admin" and int(row["disabled"]) == 0
        removes = delete or disable or (role is not None and role != "admin")
        if active and removes and self._active_admin_count(conn) <= 1:
            raise LastAdminError("At least one active admin is required.")
        return row

    def _create_admin(self, username: str, password_hash: str, now: datetime) -> None:
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone() is not None:
                raise ConfigWriteError(["An admin already exists."])
            conn.execute(
                """INSERT INTO users(
                       username, password_hash, role, disabled, created_at, failed_count, locked_until
                   ) VALUES (?, ?, 'admin', 0, ?, 0, NULL)""",
                (username, password_hash, dump_dt(now)),
            )
            self._audit(conn, username, "setup", username, {}, now)

    def _create_user(
        self, actor: str, now: datetime, username: str, password_hash: str, role: str
    ) -> None:
        if role not in {"admin", "member"}:
            raise ConfigWriteError(["role: choose admin or member"])
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone() is not None:
                raise ConfigWriteError(["That username is already in use."])
            conn.execute(
                """INSERT INTO users(
                       username, password_hash, role, disabled, created_at, failed_count, locked_until
                   ) VALUES (?, ?, ?, 0, ?, 0, NULL)""",
                (username, password_hash, role, dump_dt(now)),
            )
            self._audit(conn, actor, "user_created", username, {"role": role}, now)

    def _set_role(self, username: str, role: str, actor: str, now: datetime) -> None:
        if role not in {"admin", "member"}:
            raise ConfigWriteError(["role: choose admin or member"])
        with self._tx() as conn:
            self._guard_last_admin(conn, username, role=role)
            conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
            self._audit(conn, actor, "user_role", username, {"role": role}, now)

    def _set_disabled(self, username: str, disabled: bool, actor: str, now: datetime) -> None:
        with self._tx() as conn:
            self._guard_last_admin(conn, username, disable=disabled)
            conn.execute(
                "UPDATE users SET disabled = ? WHERE username = ?",
                (1 if disabled else 0, username),
            )
            if disabled:
                conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
            self._audit(
                conn,
                actor,
                "user_disabled" if disabled else "user_enabled",
                username,
                {"disabled": disabled},
                now,
            )

    def _delete_user(self, username: str, actor: str, now: datetime) -> None:
        with self._tx() as conn:
            self._guard_last_admin(conn, username, delete=True)
            conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
            conn.execute("DELETE FROM users WHERE username = ?", (username,))
            self._audit(conn, actor, "user_deleted", username, {}, now)

    def _set_password(
        self, username: str, password_hash: str, now: datetime, keep_token: str = ""
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """UPDATE users SET password_hash = ?, failed_count = 0, locked_until = NULL
                   WHERE username = ?""",
                (password_hash, username),
            )
            digest = hash_token(keep_token) if keep_token else ""
            conn.execute(
                "DELETE FROM sessions WHERE username = ? AND token_hash != ?",
                (username, digest),
            )
            self._audit(conn, username, "password_changed", username, {"password": "updated"}, now)

    def _reset_password(
        self, actor: str, username: str, password_hash: str, now: datetime, keep_token: str = ""
    ) -> None:
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone() is None:
                raise KeyError(username)
            conn.execute(
                """UPDATE users SET password_hash = ?, failed_count = 0, locked_until = NULL
                   WHERE username = ?""",
                (password_hash, username),
            )
            digest = hash_token(keep_token) if keep_token else ""
            conn.execute(
                "DELETE FROM sessions WHERE username = ? AND token_hash != ?",
                (username, digest),
            )
            self._audit(conn, actor, "password_reset", username, {"password": "updated"}, now)

    def _delete_admin(self, now: datetime) -> bool:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT username FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1"
            ).fetchone()
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM users")
            if row is None:
                self._audit(conn, "cli", "reset_admin", "none", {"result": "empty"}, now)
                return False
            self._audit(conn, "cli", "reset_admin", row["username"], {}, now)
            return True

    def _prune_attempts(self, conn: sqlite3.Connection, now: datetime) -> None:
        cutoff = dump_dt(now - timedelta(days=2))
        moment = dump_dt(now)
        conn.execute(
            """DELETE FROM login_attempts
               WHERE attempted_at < ? AND (locked_until IS NULL OR locked_until <= ?)""",
            (cutoff, moment),
        )

    def _login_is_locked(self, username: str, ip: str, now: datetime) -> bool:
        moment = now.astimezone(UTC)
        with self._lock:
            account = self._conn.execute(
                "SELECT locked_until FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if account is not None and account["locked_until"] and load_dt(account["locked_until"]) > moment:
                return True
            rows = self._conn.execute(
                """SELECT username, ip, lock_kind, locked_until FROM login_attempts
                   WHERE locked_until IS NOT NULL AND (username = ? OR ip = ?)""",
                (username, ip),
            ).fetchall()
        for row in rows:
            if load_dt(row["locked_until"]) <= moment:
                continue
            kind = row["lock_kind"]
            if kind in {"user", "both"} and row["username"] == username:
                return True
            if kind in {"ip", "both"} and row["ip"] == ip:
                return True
        return False

    def _record_failure(self, username: str, ip: str, now: datetime) -> bool:
        with self._tx() as conn:
            self._prune_attempts(conn, now)
            conn.execute(
                """INSERT INTO login_attempts(ip, username, attempted_at, lock_kind, locked_until)
                   VALUES (?, ?, ?, NULL, NULL)""",
                (ip, username, dump_dt(now)),
            )
            attempt_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            window_start = dump_dt(now - LOCK_WINDOW)
            user_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM login_attempts WHERE username = ? AND attempted_at > ?",
                    (username, window_start),
                ).fetchone()[0]
            )
            ip_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM login_attempts WHERE ip = ? AND attempted_at > ?",
                    (ip, window_start),
                ).fetchone()[0]
            )
            user_locked = user_count >= 5
            ip_locked = ip_count >= 5
            if user_locked and ip_locked:
                kind = "both"
            elif user_locked:
                kind = "user"
            elif ip_locked:
                kind = "ip"
            else:
                kind = None
            if kind is not None:
                until = dump_dt(now + LOCK_WINDOW)
                conn.execute(
                    "UPDATE login_attempts SET lock_kind = ?, locked_until = ? WHERE id = ?",
                    (kind, until, attempt_id),
                )
            account = conn.execute(
                "SELECT username FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if account is not None:
                if user_locked:
                    conn.execute(
                        "UPDATE users SET failed_count = ?, locked_until = ? WHERE username = ?",
                        (user_count, dump_dt(now + LOCK_WINDOW), username),
                    )
                else:
                    conn.execute(
                        "UPDATE users SET failed_count = ? WHERE username = ?",
                        (user_count, username),
                    )
            return kind is not None

    def _clear_failures(self, username: str, ip: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE users SET failed_count = 0, locked_until = NULL WHERE username = ?",
                (username,),
            )
            conn.execute(
                "DELETE FROM login_attempts WHERE username = ? OR ip = ?",
                (username, ip),
            )

    def _create_session(
        self, username: str, now: datetime, remember: bool = False, user_agent: str = ""
    ) -> tuple[str, str]:
        token = new_token()
        csrf = new_token()
        lifetime = REMEMBER_SESSION if remember else SHORT_SESSION
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO sessions(
                       token_hash, csrf, username, created_at, expires_at, last_seen, remember, user_agent
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    hash_token(token),
                    csrf,
                    username,
                    dump_dt(now),
                    dump_dt(now + lifetime),
                    dump_dt(now),
                    1 if remember else 0,
                    str(user_agent or "")[:80],
                ),
            )
        return token, csrf

    def _session_from(self, row: sqlite3.Row, seen: datetime, role: str) -> Session:
        return Session(
            row["token_hash"],
            row["csrf"],
            row["username"],
            load_dt(row["created_at"]),
            load_dt(row["expires_at"]),
            seen,
            role,
        )

    def _use_session(self, token: str, now: datetime) -> Session | None:
        if not token:
            return None
        digest = hash_token(token)
        moment = now.astimezone(UTC)
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (digest,)).fetchone()
            if row is None:
                return None
            account = conn.execute(
                "SELECT role, disabled FROM users WHERE username = ?",
                (row["username"],),
            ).fetchone()
            expires = load_dt(row["expires_at"])
            seen = load_dt(row["last_seen"])
            remember = bool(row["remember"])
            idle_hit = (not remember) and moment - seen >= IDLE_TIMEOUT
            if account is None or int(account["disabled"]) == 1 or moment >= expires or idle_hit:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
                return None
            if moment - seen >= TOUCH_AFTER:
                if remember:
                    conn.execute(
                        "UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
                        (dump_dt(moment), dump_dt(moment + REMEMBER_SESSION), digest),
                    )
                else:
                    conn.execute(
                        "UPDATE sessions SET last_seen = ? WHERE token_hash = ?",
                        (dump_dt(moment), digest),
                    )
            return self._session_from(row, moment if moment - seen >= TOUCH_AFTER else seen, account["role"])

    def _list_sessions(self, current_token: str, username: str | None) -> list[SessionInfo]:
        digest = hash_token(current_token) if current_token else ""
        sql = """SELECT rowid AS id, username, created_at, last_seen, expires_at, user_agent, remember, token_hash
                 FROM sessions"""
        params: tuple[object, ...] = ()
        if username is not None:
            sql += " WHERE username = ?"
            params = (username,)
        sql += " ORDER BY last_seen DESC, rowid DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            SessionInfo(
                int(row["id"]),
                row["username"],
                load_dt(row["created_at"]),
                load_dt(row["last_seen"]),
                load_dt(row["expires_at"]),
                row["user_agent"] or "Browser on Unknown",
                row["token_hash"] == digest,
                bool(row["remember"]),
            )
            for row in rows
        ]

    def _revoke_session(self, session_id: int, actor: str, now: datetime, *, username: str, admin: bool) -> str:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT rowid, username FROM sessions WHERE rowid = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            if not admin and row["username"] != username:
                raise PermissionError(session_id)
            conn.execute("DELETE FROM sessions WHERE rowid = ?", (session_id,))
            self._audit(conn, actor, "session_revoked", row["username"], {"session": session_id}, now)
            return str(row["username"])

    def _delete_session(self, token: str) -> None:
        if not token:
            return
        with self._tx() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))

    def _delete_user_sessions(self, username: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM sessions WHERE username = ?", (username,))

    def _prune_form_tokens(self, conn: sqlite3.Connection, now: datetime) -> None:
        conn.execute("DELETE FROM form_tokens WHERE expires_at <= ?", (dump_dt(now),))

    def _create_form_token(self, now: datetime) -> str:
        token = new_token()
        with self._tx() as conn:
            self._prune_form_tokens(conn, now)
            conn.execute(
                "INSERT INTO form_tokens(token_hash, expires_at) VALUES (?, ?)",
                (hash_token(token), dump_dt(now + timedelta(hours=1))),
            )
        return token

    def _consume_form_token(self, token: str, now: datetime) -> bool:
        if not token:
            return False
        digest = hash_token(token)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT expires_at FROM form_tokens WHERE token_hash = ?",
                (digest,),
            ).fetchone()
            conn.execute("DELETE FROM form_tokens WHERE token_hash = ?", (digest,))
            self._prune_form_tokens(conn, now)
        if row is None:
            return False
        return load_dt(row["expires_at"]) > now.astimezone(UTC)

    def _child_rows(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(conn.execute("SELECT * FROM children ORDER BY id").fetchall())

    def _child_record(self, row: sqlite3.Row) -> ChildRecord:
        return ChildRecord(
            int(row["id"]),
            row["label"],
            row["skylight_profile"],
            row["object_name"],
            row["cutoff"],
        )

    def _list_children(self) -> list[ChildRecord]:
        with self._lock:
            return [self._child_record(row) for row in self._child_rows(self._conn)]

    def _payload(self, conn: sqlite3.Connection, children: list[object] | None = None) -> dict[str, object]:
        settings = self._settings_map(conn)
        rows = self._child_rows(conn) if children is None else children
        return {
            "timezone": settings.get("timezone", "America/Chicago"),
            "poll_seconds": settings.get("poll_seconds", 60),
            "dry_run": settings.get("dry_run", True),
            "include_late": settings.get("include_late", True),
            "include_routines": settings.get("include_routines", True),
            "empty_day_unlocks": settings.get("empty_day_unlocks", False),
            "unifi_url": settings.get("unifi_url", ""),
            "unifi_site": settings.get("unifi_site"),
            "unifi_tls_ca": settings.get("unifi_tls_ca", ""),
            "children": [
                {
                    "name": row["label"],
                    "skylight_profile": row["skylight_profile"],
                    "object_name": row["object_name"],
                    "cutoff": row["cutoff"],
                    "manual_unblock": False,
                }
                for row in rows
            ],
        }

    def _read_config(self) -> AppConfig:
        with self._lock:
            return AppConfig.model_validate(self._payload(self._conn))

    def _validated_settings(self, changes: dict[str, object]) -> AppConfig:
        unknown = set(changes) - set(SETTING_KEYS)
        if unknown:
            raise ConfigWriteError([f"Unknown setting: {name}" for name in sorted(unknown)])
        with self._lock:
            payload = self._payload(self._conn)
        payload.update(changes)
        return AppConfig.model_validate(payload)

    def _update_settings(self, actor: str, now: datetime, changes: dict[str, object]) -> None:
        config = self._validated_settings(changes)
        saved = {
            "timezone": config.timezone,
            "poll_seconds": config.poll_seconds,
            "dry_run": config.dry_run,
            "include_late": config.include_late,
            "include_routines": config.include_routines,
            "empty_day_unlocks": config.empty_day_unlocks,
            "unifi_url": config.unifi_url,
            "unifi_site": config.unifi_site,
            "unifi_tls_ca": config.unifi_tls_ca,
        }
        diff: dict[str, object] = {}
        for key in changes:
            diff[key] = "updated" if key == "unifi_tls_ca" else saved[key]
        with self._tx() as conn:
            for key in changes:
                self._put(conn, key, saved[key])
            self._bump(conn)
            self._audit(conn, actor, "settings_updated", "settings", diff, now)

    def _write_settings(self, conn: sqlite3.Connection, config: AppConfig) -> None:
        values: dict[str, object] = {
            "timezone": config.timezone,
            "poll_seconds": config.poll_seconds,
            "dry_run": config.dry_run,
            "include_late": config.include_late,
            "include_routines": config.include_routines,
            "empty_day_unlocks": config.empty_day_unlocks,
            "unifi_url": config.unifi_url,
            "unifi_site": config.unifi_site,
            "unifi_tls_ca": config.unifi_tls_ca,
        }
        for key, value in values.items():
            self._put(conn, key, value)

    def _cutoff_text(self, config: AppConfig, label: str) -> str:
        for child in config.children:
            if child.name == label:
                return f"{child.cutoff.hour:02d}:{child.cutoff.minute:02d}"
        raise ConfigWriteError(["Child could not be saved."])

    def _add_child(
        self,
        actor: str,
        now: datetime,
        label: str,
        profile: str,
        object_name: str,
        cutoff: str,
    ) -> ChildRecord:
        with self._lock:
            rows = [_row_dict(row) for row in self._child_rows(self._conn)]
            rows.append(
                {
                    "label": label,
                    "skylight_profile": profile,
                    "object_name": object_name,
                    "cutoff": cutoff,
                }
            )
            config = AppConfig.model_validate(self._payload(self._conn, rows))
            stored_cutoff = self._cutoff_text(config, label)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    """INSERT INTO children(label, skylight_profile, object_name, cutoff)
                       VALUES (?, ?, ?, ?)""",
                    (label, profile, object_name, stored_cutoff),
                )
                self._bump(self._conn)
                self._audit(
                    self._conn,
                    actor,
                    "child_created",
                    label,
                    {
                        "label": label,
                        "skylight_profile": profile,
                        "object_name": object_name,
                        "cutoff": stored_cutoff,
                    },
                    now,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            child_id = int(cursor.lastrowid or 0)
        return ChildRecord(child_id, label, profile, object_name, stored_cutoff)

    def _update_child(
        self,
        child_id: int,
        actor: str,
        now: datetime,
        label: str,
        profile: str,
        object_name: str,
        cutoff: str,
    ) -> None:
        with self._lock:
            rows = [_row_dict(row) for row in self._child_rows(self._conn)]
            found = False
            updated: list[dict[str, object]] = []
            for row in rows:
                if int(row["id"]) == child_id:
                    found = True
                    updated.append(
                        {
                            "label": label,
                            "skylight_profile": profile,
                            "object_name": object_name,
                            "cutoff": cutoff,
                        }
                    )
                else:
                    updated.append(row)
            if not found:
                raise KeyError(child_id)
            config = AppConfig.model_validate(self._payload(self._conn, updated))
            stored_cutoff = self._cutoff_text(config, label)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """UPDATE children
                       SET label = ?, skylight_profile = ?, object_name = ?, cutoff = ?
                       WHERE id = ?""",
                    (label, profile, object_name, stored_cutoff, child_id),
                )
                self._bump(self._conn)
                self._audit(
                    self._conn,
                    actor,
                    "child_updated",
                    label,
                    {
                        "label": label,
                        "skylight_profile": profile,
                        "object_name": object_name,
                        "cutoff": stored_cutoff,
                    },
                    now,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _delete_child(self, child_id: int, actor: str, now: datetime) -> None:
        with self._lock:
            rows = [_row_dict(row) for row in self._child_rows(self._conn)]
            target = next((row for row in rows if int(row["id"]) == child_id), None)
            if target is None:
                raise KeyError(child_id)
            if len(rows) <= 1:
                raise ConfigWriteError(["At least one child is required."])
            remaining = [row for row in rows if int(row["id"]) != child_id]
            AppConfig.model_validate(self._payload(self._conn, remaining))
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM overrides WHERE child_id = ?", (child_id,))
                self._conn.execute("DELETE FROM children WHERE id = ?", (child_id,))
                self._bump(self._conn)
                self._audit(
                    self._conn,
                    actor,
                    "child_deleted",
                    str(target["label"]),
                    {"label": target["label"]},
                    now,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _read_secret(
        self, conn: sqlite3.Connection, name: str
    ) -> tuple[str | None, str, str | None, str]:
        row = conn.execute(
            "SELECT nonce, ciphertext, updated_at FROM secrets WHERE name = ?",
            (name,),
        ).fetchone()
        env = os.environ.get(name) or None
        note = ""
        if row is not None and self._key is not None:
            try:
                value, legacy = decrypt(self._key, row["nonce"], row["ciphertext"], name)
            except Exception:  # noqa: BLE001 - decrypt failures must not include the secret
                LOGGER.warning("secret decrypt failed name=%s", name)
                note = "The stored value could not be read."
            else:
                if legacy:
                    self._rewrite_secret(conn, name, value)
                REDACTOR.update([value])
                return value, "db", row["updated_at"], ""
        elif row is not None:
            note = "A database value is present, but APP_ENCRYPTION_KEY is not set."
        if env:
            REDACTOR.update([env])
            return env, "env", None, note
        return None, "missing", None, note

    def _rewrite_secret(self, conn: sqlite3.Connection, name: str, plaintext: str) -> None:
        if self._key is None:
            return
        nonce, ciphertext = encrypt(self._key, plaintext, name)
        conn.execute(
            "UPDATE secrets SET nonce = ?, ciphertext = ? WHERE name = ?",
            (nonce, ciphertext, name),
        )
        LOGGER.info("secret migrated to authenticated encryption name=%s", name)

    def _rotate_key(self, new_key: bytes) -> int:
        if self._key is None:
            raise RuntimeError("APP_ENCRYPTION_KEY is not set")
        if len(new_key) != 32:
            raise RuntimeError("APP_ENCRYPTION_KEY_NEW must be base64-encoded 32 bytes")
        with self._tx() as conn:
            rows = conn.execute("SELECT name, nonce, ciphertext FROM secrets").fetchall()
            rewritten: list[tuple[str, bytes, bytes]] = []
            for row in rows:
                try:
                    value, _legacy = decrypt(self._key, row["nonce"], row["ciphertext"], row["name"])
                except Exception:  # noqa: BLE001 - do not include the secret in the error
                    raise RuntimeError(f"secret could not be rotated name={row['name']}") from None
                REDACTOR.update([value])
                nonce, ciphertext = encrypt(new_key, value, row["name"])
                rewritten.append((row["name"], nonce, ciphertext))
            for name, nonce, ciphertext in rewritten:
                conn.execute(
                    "UPDATE secrets SET nonce = ?, ciphertext = ? WHERE name = ?",
                    (nonce, ciphertext, name),
                )
        self._key = new_key
        return len(rewritten)

    def _secret_statuses(self) -> list[SecretStatus]:
        with self._lock:
            statuses: list[SecretStatus] = []
            for name in SECRET_NAMES:
                value, source, updated, note = self._read_secret(self._conn, name)
                del value
                state = "set" if source in {"db", "env"} else "not set"
                shown = source if source in {"db", "env"} else ""
                statuses.append(SecretStatus(name, state, shown, updated, note))
            return statuses

    def _resolve_secrets(self) -> ResolvedSecrets:
        with self._lock:
            values: dict[str, str] = {}
            sources: dict[str, str] = {}
            for name in SECRET_NAMES:
                value, source, _updated, _note = self._read_secret(self._conn, name)
                values[name] = value or ""
                sources[name] = source
        resolved = ResolvedSecrets(
            values["SKYLIGHT_EMAIL"],
            values["SKYLIGHT_PASSWORD"],
            values["UNIFI_API_KEY"],
            sources,
        )
        REDACTOR.update(resolved.materials())
        return resolved

    def _set_secret(self, name: str, plaintext: str, actor: str, now: datetime) -> bool:
        if name not in SECRET_NAMES:
            raise KeyError(name)
        if plaintext == "":
            return False
        if not self.encryption_enabled or self._key is None:
            raise ConfigWriteError(["Secret storage is disabled."])
        REDACTOR.update([plaintext])
        nonce, ciphertext = encrypt(self._key, plaintext, name)
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO secrets(name, nonce, ciphertext, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     nonce = excluded.nonce,
                     ciphertext = excluded.ciphertext,
                     updated_at = excluded.updated_at""",
                (name, nonce, ciphertext, dump_dt(now)),
            )
            self._bump(conn)
            self._audit(
                conn,
                actor,
                "secret_updated",
                name,
                {"secret": name, "change": "updated"},
                now,
            )
        return True

    def _clear_secret(self, name: str, actor: str, now: datetime) -> bool:
        if name not in SECRET_NAMES:
            raise KeyError(name)
        with self._lock:
            row = self._conn.execute(
                "SELECT nonce, ciphertext FROM secrets WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None:
                return False
            if self._key is not None:
                try:
                    value, _legacy = decrypt(self._key, row["nonce"], row["ciphertext"], name)
                    REDACTOR.update([value])
                except Exception:  # noqa: BLE001 - decrypt failures must not include the secret
                    LOGGER.warning("secret decrypt failed name=%s", name)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM secrets WHERE name = ?", (name,))
                self._bump(self._conn)
                self._audit(
                    self._conn,
                    actor,
                    "secret_cleared",
                    name,
                    {"secret": name, "change": "cleared"},
                    now,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return True

    def _override_from(self, row: sqlite3.Row) -> ActiveOverride:
        return ActiveOverride(
            int(row["id"]),
            int(row["child_id"]),
            row["label"],
            row["action"],
            load_dt(row["expires_at"]),
            row["set_by"],
            load_dt(row["created_at"]),
        )

    def _override_select(self) -> str:
        return """SELECT o.id, o.child_id, c.label, o.action, o.expires_at, o.set_by, o.created_at
                  FROM overrides o JOIN children c ON c.id = o.child_id"""

    def _replace_override(
        self,
        conn: sqlite3.Connection,
        child_id: int,
        label: str,
        action: str,
        expires: datetime,
        actor: str,
        now: datetime,
    ) -> None:
        existing = conn.execute(
            "SELECT id, action FROM overrides WHERE child_id = ? AND cleared_at IS NULL",
            (child_id,),
        ).fetchone()
        replaced = None
        if existing is not None:
            conn.execute(
                """UPDATE overrides
                   SET cleared_at = ?, cleared_by = ?, clear_reason = 'replaced'
                   WHERE id = ?""",
                (dump_dt(now), actor, existing["id"]),
            )
            replaced = int(existing["id"])
            self._audit(
                conn,
                actor,
                "override_replaced",
                label,
                {"action": existing["action"], "clear_reason": "replaced"},
                now,
            )
        conn.execute(
            """INSERT INTO overrides(child_id, action, expires_at, set_by, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (child_id, action, dump_dt(expires), actor, dump_dt(now)),
        )
        self._audit(
            conn,
            actor,
            "override_set",
            label,
            {"action": action, "expires_at": dump_dt(expires), "replaced": replaced},
            now,
        )

    def _set_override(
        self, child_id: int, action: str, actor: str, now: datetime, confirmation: str
    ) -> ActiveOverride:
        if action not in {"unblock", "block"}:
            raise OverrideError("Unknown action.")
        with self._tx() as conn:
            child = conn.execute("SELECT * FROM children WHERE id = ?", (child_id,)).fetchone()
            if child is None:
                raise KeyError(child_id)
            timezone = str(self._setting(conn, "timezone", "America/Chicago"))
            cutoff = time.fromisoformat(str(child["cutoff"]))
            expires = override_expiry(now, cutoff, timezone)
            if expires <= now.astimezone(UTC):
                raise OverrideError("The override would expire immediately.")
            local = now.astimezone(ZoneInfo(timezone))
            if (
                action == "unblock"
                and past_cutoff(cutoff, local)
                and not constant_equal(confirmation, UNBLOCK_CONFIRMATION)
            ):
                raise ConfirmationRequired(expires)
            self._replace_override(conn, child_id, child["label"], action, expires, actor, now)
            row = conn.execute(
                self._override_select() + " WHERE o.child_id = ? AND o.cleared_at IS NULL",
                (child_id,),
            ).fetchone()
        if row is None:
            raise OverrideError("The override was not stored.")
        return self._override_from(row)

    def _clear_override(self, child_id: int, actor: str, now: datetime) -> bool:
        with self._tx() as conn:
            row = conn.execute(
                """SELECT o.id, o.action, c.label FROM overrides o
                   JOIN children c ON c.id = o.child_id
                   WHERE o.child_id = ? AND o.cleared_at IS NULL""",
                (child_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """UPDATE overrides
                   SET cleared_at = ?, cleared_by = ?, clear_reason = 'manual'
                   WHERE id = ?""",
                (dump_dt(now), actor, row["id"]),
            )
            self._audit(
                conn,
                actor,
                "override_cleared",
                row["label"],
                {"action": row["action"], "clear_reason": "manual"},
                now,
            )
        return True

    def _expire_due(self, now: datetime) -> list[ActiveOverride]:
        moment = now.astimezone(UTC)
        expired: list[ActiveOverride] = []
        with self._tx() as conn:
            rows = conn.execute(
                self._override_select() + " WHERE o.cleared_at IS NULL"
            ).fetchall()
            for row in rows:
                if load_dt(row["expires_at"]) > moment:
                    continue
                conn.execute(
                    """UPDATE overrides
                       SET cleared_at = ?, cleared_by = 'system', clear_reason = 'expired'
                       WHERE id = ?""",
                    (dump_dt(moment), row["id"]),
                )
                self._audit(
                    conn,
                    "system",
                    "override_expired",
                    row["label"],
                    {
                        "action": row["action"],
                        "clear_reason": "expired",
                        "expires_at": row["expires_at"],
                    },
                    now,
                )
                expired.append(self._override_from(row))
        return expired

    def _active_override(self, label: str, now: datetime) -> ActiveOverride | None:
        moment = now.astimezone(UTC)
        with self._lock:
            row = self._conn.execute(
                self._override_select() + " WHERE o.cleared_at IS NULL AND c.label = ?",
                (label,),
            ).fetchone()
        if row is None or load_dt(row["expires_at"]) <= moment:
            return None
        return self._override_from(row)

    def _list_overrides(self, now: datetime) -> list[ActiveOverride]:
        moment = now.astimezone(UTC)
        with self._lock:
            rows = self._conn.execute(
                self._override_select() + " WHERE o.cleared_at IS NULL"
            ).fetchall()
        return [self._override_from(row) for row in rows if load_dt(row["expires_at"]) > moment]

    def _imported_hash(self) -> str | None:
        with self._lock:
            value = self._setting(self._conn, IMPORT_KEY)
        return value if isinstance(value, str) else None

    def _is_imported(self) -> bool:
        return self._imported_hash() is not None

    def _import_config(self, config: AppConfig, now: datetime, fingerprint: str) -> bool:
        with self._lock:
            if self._setting(self._conn, IMPORT_KEY) is not None:
                return False
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if self._setting(self._conn, IMPORT_KEY) is not None:
                    self._conn.execute("ROLLBACK")
                    return False
                self._write_settings(self._conn, config)
                labels: list[str] = []
                migrated: list[str] = []
                for child in config.children:
                    cursor = self._conn.execute(
                        """INSERT INTO children(label, skylight_profile, object_name, cutoff)
                           VALUES (?, ?, ?, ?)""",
                        (
                            child.name,
                            child.skylight_profile,
                            child.object_name,
                            f"{child.cutoff.hour:02d}:{child.cutoff.minute:02d}",
                        ),
                    )
                    labels.append(child.name)
                    if not child.manual_unblock:
                        continue
                    expires = override_expiry(now, child.cutoff, config.timezone)
                    if expires <= now.astimezone(UTC):
                        continue
                    child_id = int(cursor.lastrowid or 0)
                    self._replace_override(
                        self._conn, child_id, child.name, "unblock", expires, "import", now
                    )
                    migrated.append(child.name)
                self._put(self._conn, IMPORT_KEY, fingerprint)
                self._bump(self._conn)
                self._audit(
                    self._conn,
                    "import",
                    "import",
                    "config",
                    {"children": labels, "manual_unblock": migrated},
                    now,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return True

    def _add_audit(
        self, actor: str, action: str, target: str, diff: dict[str, object], now: datetime
    ) -> None:
        with self._tx() as conn:
            self._audit(conn, actor, action, target, diff, now)

    def _list_audit(self, page: int, page_size: int = 25) -> AuditPage:
        page = max(1, int(page))
        page_size = max(1, int(page_size))
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0])
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            offset = (page - 1) * page_size
            rows = self._conn.execute(
                """SELECT ts, actor, action, target, diff FROM audit
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (page_size, offset),
            ).fetchall()
        return AuditPage(
            [AuditRow(row["ts"], row["actor"], row["action"], row["target"], row["diff"]) for row in rows],
            page,
            pages,
            total,
        )


def _row_dict(row: sqlite3.Row) -> dict[str, object]:
    return dict(zip(row.keys(), tuple(row), strict=True))


_ASYNC_METHODS = (
    "_get_user",
    "_get_admin",
    "_list_users",
    "_create_admin",
    "_create_user",
    "_set_role",
    "_set_disabled",
    "_delete_user",
    "_set_password",
    "_reset_password",
    "_delete_admin",
    "_login_is_locked",
    "_record_failure",
    "_clear_failures",
    "_create_session",
    "_use_session",
    "_delete_session",
    "_delete_user_sessions",
    "_list_sessions",
    "_revoke_session",
    "_create_form_token",
    "_consume_form_token",
    "_is_imported",
    "_imported_hash",
    "_import_config",
    "_config_version",
    "_read_config",
    "_update_settings",
    "_list_children",
    "_add_child",
    "_update_child",
    "_delete_child",
    "_secret_statuses",
    "_set_secret",
    "_clear_secret",
    "_rotate_key",
    "_resolve_secrets",
    "_set_override",
    "_clear_override",
    "_expire_due",
    "_active_override",
    "_list_overrides",
    "_add_audit",
    "_list_audit",
    "_journal_mode",
)


def _as_async(sync_name: str):
    async def wrapper(self: Store, *args: object, **kwargs: object) -> object:
        return await asyncio.to_thread(getattr(self, sync_name), *args, **kwargs)

    wrapper.__name__ = sync_name[1:]
    return wrapper


for _sync_name in _ASYNC_METHODS:
    setattr(Store, _sync_name[1:], _as_async(_sync_name))
