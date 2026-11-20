from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta

from starlette.testclient import TestClient
from tests.uiutil import (
    FAST,
    ORIGIN,
    PASSWORD,
    SAME_SITE,
    SETUP_TOKEN,
    USERNAME,
    build,
    csrf_from,
    db_rows,
    login,
    sign_in,
)

from skylight_wifi_automation.security import hash_token
from skylight_wifi_automation.store import LastAdminError, Store
from skylight_wifi_automation.timeutil import dump_dt
from skylight_wifi_automation.web import PLAIN_SESSION_COOKIE, AppContext, create_app

MEMBER = "casey"
MEMBER_PASSWORD = "member-password-1"


def _member(bundle) -> None:
    asyncio.run(
        bundle.store.create_user(USERNAME, bundle.clock(), MEMBER, FAST.hash(MEMBER_PASSWORD), "member")
    )


def _ids(html: str) -> list[str]:
    import re

    return re.findall(r'action="/sessions/([^"]+)/revoke"', html)


def _current(html: str) -> str:
    import re

    match = re.search(r'data-current="yes".*?action="/sessions/([^"]+)/revoke"', html, re.DOTALL)
    assert match, html
    return match.group(1)


def test_member_can_block_but_not_open_admin_pages(tmp_path):
    bundle = build(tmp_path)
    _member(bundle)
    child_id = db_rows(bundle.store.path, "SELECT id FROM children LIMIT 1")[0]["id"]
    with TestClient(bundle.app) as client:
        assert login(client, username=MEMBER, password=MEMBER_PASSWORD).status_code == 303
        home = client.get("/")
        assert home.status_code == 200
        assert "child-a" in home.text
        for path in ("/secrets", "/users", "/children", "/settings", "/unifi", "/audit"):
            assert client.get(path).status_code == 403, path
        token = csrf_from(home.text)
        blocked = client.post(
            "/secrets",
            data={"csrf_token": token, "UNIFI_API_KEY": "should-not-save"},
            headers=ORIGIN,
            follow_redirects=False,
        )
        schedule = client.post(
            "/children",
            data={
                "csrf_token": token,
                "label": "extra",
                "skylight_profile": "extra",
                "object_name": "No Internet - extra",
                "cutoff": "21:00",
            },
            headers=ORIGIN,
            follow_redirects=False,
        )
        changed = client.post(
            f"/overrides/{child_id}",
            data={"csrf_token": token, "action": "block"},
            headers=ORIGIN,
            follow_redirects=False,
        )
    assert blocked.status_code == 403
    assert "should-not-save" not in blocked.text
    assert schedule.status_code == 403
    assert changed.status_code == 303
    assert db_rows(bundle.store.path, "SELECT action FROM overrides WHERE cleared_at IS NULL")[0]["action"] == "block"
    assert db_rows(bundle.store.path, "SELECT label FROM children WHERE label = 'extra'") == []
    audit = " ".join(row["actor"] for row in db_rows(bundle.store.path, "SELECT actor FROM audit"))
    assert MEMBER in audit


def test_last_admin_cannot_remove_themselves(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        token = sign_in(client)
        for path, data in (
            (f"/users/{USERNAME}/delete", {}),
            (f"/users/{USERNAME}/disable", {}),
            (f"/users/{USERNAME}/role", {"role": "member"}),
        ):
            response = client.post(
                path,
                data={"csrf_token": token, **data},
                headers=ORIGIN,
                follow_redirects=False,
            )
            assert response.status_code == 400, path
            assert "active admin" in response.text
            token = csrf_from(response.text)
    stored = db_rows(bundle.store.path, "SELECT role, disabled FROM users")
    assert len(stored) == 1
    assert stored[0]["role"] == "admin"
    assert stored[0]["disabled"] == 0


def test_concurrent_demotion_leaves_one_admin(tmp_path):
    bundle = build(tmp_path)

    async def run() -> list[object]:
        now = bundle.clock()
        await bundle.store.create_admin("ada", FAST.hash(PASSWORD), now)
        await bundle.store.create_user("ada", now, "blair", FAST.hash(PASSWORD), "admin")
        return list(
            await asyncio.gather(
                bundle.store.set_role("ada", "member", "ada", now),
                bundle.store.set_role("blair", "member", "blair", now),
                return_exceptions=True,
            )
        )

    results = asyncio.run(run())
    assert any(isinstance(item, LastAdminError) for item in results)
    assert any(item is None for item in results)
    active = db_rows(
        bundle.store.path,
        "SELECT username FROM users WHERE role = 'admin' AND disabled = 0",
    )
    assert len(active) == 1


def test_session_survives_rebuilding_the_app(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        sign_in(client)
        cookie = client.cookies.get(PLAIN_SESSION_COOKIE)
        assert cookie
        assert client.get("/").status_code == 200
    path = bundle.store.path
    key = bundle.store._key
    assert cookie.encode() not in path.read_bytes()
    assert hash_token(cookie) == db_rows(path, "SELECT token_hash FROM sessions")[0]["token_hash"]
    bundle.store.close()
    store = Store(path, key)
    previous = bundle.app.state.context
    app = create_app(
        AppContext(
            store=store,
            service=previous.service,
            source=previous.source,
            clock=previous.clock,
            setup_token=previous.setup_token,
            cookie_secure=previous.cookie_secure,
            hasher=previous.hasher,
            dummy_hash=previous.dummy_hash,
            tester=previous.tester,
            read_only=previous.read_only,
        )
    )
    try:
        with TestClient(app) as client:
            client.cookies.set(PLAIN_SESSION_COOKIE, cookie)
            page = client.get("/")
            assert page.status_code == 200
            assert "child-a" in page.text
    finally:
        store.close()


def test_remember_me_slides_and_plain_sessions_stay_short(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        sign_in(client)
        client.post("/logout", data={"csrf_token": csrf_from(client.get("/").text)}, headers=SAME_SITE)
        page = client.get("/login")
        signed = client.post(
            "/login",
            data={
                "csrf_token": csrf_from(page.text),
                "username": USERNAME,
                "password": PASSWORD,
                "remember": "1",
            },
            headers=SAME_SITE,
            follow_redirects=False,
        )
        assert signed.status_code == 303
        bundle.clock.moment += timedelta(days=10)
        assert client.get("/").status_code == 200
        expires = db_rows(bundle.store.path, "SELECT expires_at, last_seen FROM sessions")[0]
        assert expires["expires_at"] >= dump_dt(bundle.clock.moment + timedelta(days=29))
        seen = dump_dt(bundle.clock.moment - timedelta(minutes=1))
        connection = sqlite3.connect(bundle.store.path)
        connection.execute("UPDATE sessions SET last_seen = ?", (seen,))
        connection.commit()
        connection.close()
        assert client.get("/sessions").status_code == 200
        assert db_rows(bundle.store.path, "SELECT last_seen FROM sessions")[0]["last_seen"] == seen
        stale = dump_dt(bundle.clock.moment - timedelta(minutes=20))
        connection = sqlite3.connect(bundle.store.path)
        connection.execute("UPDATE sessions SET last_seen = ?", (stale,))
        connection.commit()
        connection.close()
        assert client.get("/").status_code == 200
        renewed = db_rows(bundle.store.path, "SELECT last_seen, expires_at FROM sessions")[0]
        assert renewed["last_seen"] == dump_dt(bundle.clock.moment)
        assert renewed["expires_at"] == dump_dt(bundle.clock.moment + timedelta(days=30))


def test_revoke_own_session_and_not_someone_elses(tmp_path):
    bundle = build(tmp_path)
    _member(bundle)
    with TestClient(bundle.app) as owner, TestClient(bundle.app) as other, TestClient(bundle.app) as member:
        sign_in(owner)
        assert login(other).status_code == 303
        assert login(member, username=MEMBER, password=MEMBER_PASSWORD).status_code == 303
        own = owner.get("/sessions")
        own_id = _current(own.text)
        other_id = _current(other.get("/sessions").text)
        assert own_id != other_id
        revoked = owner.post(
            f"/sessions/{other_id}/revoke",
            data={"csrf_token": csrf_from(own.text)},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert revoked.status_code == 303
        denied = member.post(
            f"/sessions/{own_id}/revoke",
            data={"csrf_token": csrf_from(member.get("/sessions").text)},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert denied.status_code == 404
        assert owner.get("/", follow_redirects=False).status_code == 200
        assert other.get("/", follow_redirects=False).status_code == 303
        assert "token_hash" not in own.text


def test_admin_can_revoke_another_users_session(tmp_path):
    bundle = build(tmp_path)
    _member(bundle)
    with TestClient(bundle.app) as admin, TestClient(bundle.app) as member:
        sign_in(admin)
        assert login(member, username=MEMBER, password=MEMBER_PASSWORD).status_code == 303
        page = admin.get("/sessions")
        member_ids = _ids(member.get("/sessions").text)
        assert member_ids
        revoked = admin.post(
            f"/sessions/{member_ids[0]}/revoke",
            data={"csrf_token": csrf_from(page.text)},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert revoked.status_code == 303
        assert member.get("/", follow_redirects=False).status_code == 303
        assert admin.get("/", follow_redirects=False).status_code == 200


def test_v1_admin_row_migrates_once(tmp_path):
    path = tmp_path / "app.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version(version) VALUES (1);
        CREATE TABLE admin (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            failed_count INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT
        );
        INSERT INTO admin(username, password_hash, failed_count) VALUES ('Admin', '$argon2id$kept', 2);
        CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY,
            csrf TEXT NOT NULL,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE TABLE children (
            id INTEGER PRIMARY KEY,
            label TEXT NOT NULL,
            skylight_profile TEXT NOT NULL,
            object_name TEXT NOT NULL,
            cutoff TEXT NOT NULL
        );
        INSERT INTO children(label, skylight_profile, object_name, cutoff)
        VALUES ('Alex', 'Alex', 'No Internet - Alex', '20:30');
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO settings(key, value) VALUES ('timezone', '"America/Chicago"');
        """
    )
    now = datetime.now(UTC)
    token = "migrated-session-token"
    connection.execute(
        """INSERT INTO sessions(token_hash, csrf, username, created_at, expires_at, last_seen)
           VALUES (?, 'csrf-value', 'Admin', ?, ?, ?)""",
        (hash_token(token), dump_dt(now), dump_dt(now + timedelta(days=3)), dump_dt(now)),
    )
    connection.commit()
    connection.close()
    moment = datetime(2026, 8, 24, 18, tzinfo=UTC)
    store = Store(path, None, lambda: moment)
    try:
        account = asyncio.run(store.get_user("ADMIN"))
        assert account is not None
        assert account.username == "Admin"
        assert account.role == "admin"
        assert account.password_hash == "$argon2id$kept"
        assert account.failed_count == 2
        created = db_rows(path, "SELECT created_at FROM users")[0]["created_at"]
        assert created == dump_dt(moment)
        backups = list(path.parent.glob("app.db.bak-*"))
        assert len(backups) == 1
        assert backups[0].stat().st_mode & 0o777 == 0o600
        saved = sqlite3.connect(backups[0])
        assert saved.execute("SELECT username FROM admin").fetchone()[0] == "Admin"
        saved.close()
        session = asyncio.run(store.use_session(token, now))
        assert session is not None and session.username == "Admin"
        assert db_rows(path, "SELECT label FROM children")[0]["label"] == "Alex"
        versions = {row["version"] for row in db_rows(path, "SELECT version FROM schema_version")}
        assert versions == {1, 2}
    finally:
        store.close()
    again = Store(path, None)
    try:
        assert len(db_rows(path, "SELECT username FROM users")) == 1
        assert db_rows(path, "SELECT version FROM schema_version")[-1]["version"] == 2
    finally:
        again.close()
    assert not db_rows(path, "SELECT 1 AS present FROM sqlite_master WHERE name = 'admin'")
    assert len(list(path.parent.glob("app.db.bak-*"))) == 1


def _cookie_value(header: str) -> str:
    return header.split(";", 1)[0].split("=", 1)[1]


def test_remember_cookie_slides_and_stops_at_ninety_days(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        sign_in(client)
        client.cookies.clear()
        page = client.get("/login")
        signed = client.post(
            "/login",
            data={
                "csrf_token": csrf_from(page.text),
                "username": USERNAME,
                "password": PASSWORD,
                "remember": "1",
            },
            headers=SAME_SITE,
            follow_redirects=False,
        )
        header = signed.headers["set-cookie"]
        assert "max-age=2592000" in header.lower()
        value = _cookie_value(header)
        client.cookies.clear()
        quiet = client.get(
            "/",
            headers={"cookie": f"{PLAIN_SESSION_COOKIE}={value}"},
            follow_redirects=False,
        )
        assert quiet.status_code == 200
        assert "set-cookie" not in {name.lower() for name in quiet.headers}
        bundle.clock.moment += timedelta(days=25)
        slid = client.get(
            "/",
            headers={"cookie": f"{PLAIN_SESSION_COOKIE}={value}"},
            follow_redirects=False,
        )
        assert slid.status_code == 200
        assert "max-age=2592000" in slid.headers["set-cookie"].lower()
        bundle.clock.moment += timedelta(days=25)
        client.get(
            "/",
            headers={"cookie": f"{PLAIN_SESSION_COOKIE}={value}"},
            follow_redirects=False,
        )
        bundle.clock.moment += timedelta(days=20)
        clipped = client.get(
            "/",
            headers={"cookie": f"{PLAIN_SESSION_COOKIE}={value}"},
            follow_redirects=False,
        )
        assert clipped.status_code == 200
        clipped_age = int(clipped.headers["set-cookie"].lower().split("max-age=")[1].split(";", 1)[0])
        assert 0 < clipped_age < 2592000
        bundle.clock.moment += timedelta(days=21)
        expired = client.get(
            "/",
            headers={"cookie": f"{PLAIN_SESSION_COOKIE}={value}"},
            follow_redirects=False,
        )
    assert expired.status_code == 303


def test_short_session_cookie_has_no_max_age_and_dies_at_twelve_hours(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        sign_in(client)
        client.cookies.clear()
        page = client.get("/login")
        signed = client.post(
            "/login",
            data={"csrf_token": csrf_from(page.text), "username": USERNAME, "password": PASSWORD},
            headers=SAME_SITE,
            follow_redirects=False,
        )
        header = signed.headers["set-cookie"]
        assert "max-age" not in header.lower()
        assert "expires=" not in header.lower()
        value = _cookie_value(header)
        client.cookies.clear()
        connection = sqlite3.connect(bundle.store.path)
        connection.execute(
            "UPDATE sessions SET expires_at = ?",
            (dump_dt(bundle.clock.moment + timedelta(days=30)),),
        )
        connection.commit()
        connection.close()
        bundle.clock.moment += timedelta(hours=12, seconds=1)
        blocked = client.get(
            "/",
            headers={"cookie": f"{PLAIN_SESSION_COOKIE}={value}"},
            follow_redirects=False,
        )
    assert blocked.status_code == 303


def test_login_replaces_the_browsers_previous_session(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        sign_in(client)
        client.cookies.clear()
        page = client.get("/login")
        first = client.post(
            "/login",
            data={"csrf_token": csrf_from(page.text), "username": USERNAME, "password": PASSWORD},
            headers=SAME_SITE,
            follow_redirects=False,
        )
        old = _cookie_value(first.headers["set-cookie"])
        client.cookies.clear()
        page = client.get("/login")
        second = client.post(
            "/login",
            data={"csrf_token": csrf_from(page.text), "username": USERNAME, "password": PASSWORD},
            headers={**SAME_SITE, "cookie": f"{PLAIN_SESSION_COOKIE}={old}"},
            follow_redirects=False,
        )
        new = _cookie_value(second.headers["set-cookie"])
        assert new != old
        client.cookies.clear()
        stale = client.get(
            "/",
            headers={"cookie": f"{PLAIN_SESSION_COOKIE}={old}"},
            follow_redirects=False,
        )
        fresh = client.get(
            "/",
            headers={"cookie": f"{PLAIN_SESSION_COOKIE}={new}"},
            follow_redirects=False,
        )
    assert stale.status_code == 303
    assert fresh.status_code == 200


def test_session_ids_are_not_reused(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        sign_in(client)
        first = _current(client.get("/sessions").text)
        client.post(
            f"/sessions/{first}/revoke",
            data={"csrf_token": csrf_from(client.get("/sessions").text)},
            headers=ORIGIN,
            follow_redirects=False,
        )
        page = client.get("/login")
        client.post(
            "/login",
            data={"csrf_token": csrf_from(page.text), "username": USERNAME, "password": PASSWORD},
            headers=SAME_SITE,
            follow_redirects=False,
        )
        second = _current(client.get("/sessions").text)
        assert first != second
        missed = client.post(
            f"/sessions/{first}/revoke",
            data={"csrf_token": csrf_from(client.get("/sessions").text)},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert missed.status_code == 404
        assert client.get("/", follow_redirects=False).status_code == 200


def test_expired_sessions_are_pruned_on_login(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        sign_in(client)
        now = bundle.clock()
        connection = sqlite3.connect(bundle.store.path)
        connection.execute(
            """INSERT INTO sessions(
                   token_hash, public_id, csrf, username, created_at, expires_at, last_seen, remember, user_agent
               ) VALUES ('expired-hash', 'expired-session', 'csrf', 'admin', ?, ?, ?, 1, '')""",
            (dump_dt(now), dump_dt(now - timedelta(seconds=1)), dump_dt(now)),
        )
        connection.execute(
            """INSERT INTO sessions(
                   token_hash, public_id, csrf, username, created_at, expires_at, last_seen, remember, user_agent
               ) VALUES ('idle-hash', 'idle-session', 'csrf', 'admin', ?, ?, ?, 0, '')""",
            (
                dump_dt(now - timedelta(days=2)),
                dump_dt(now + timedelta(hours=1)),
                dump_dt(now - timedelta(hours=13)),
            ),
        )
        connection.commit()
        connection.close()
        page = client.get("/login")
        client.post(
            "/login",
            data={"csrf_token": csrf_from(page.text), "username": USERNAME, "password": PASSWORD},
            headers=SAME_SITE,
            follow_redirects=False,
        )
    left = {row["public_id"] for row in db_rows(bundle.store.path, "SELECT public_id FROM sessions")}
    assert "expired-session" not in left
    assert "idle-session" not in left
    assert left


def test_usernames_are_case_insensitive(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        sign_in(client)
        token = csrf_from(client.get("/users").text)
        created = client.post(
            "/users",
            data={
                "csrf_token": token,
                "username": "Casey",
                "password": MEMBER_PASSWORD,
                "password2": MEMBER_PASSWORD,
                "role": "member",
            },
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert created.status_code == 303
        token = csrf_from(client.get("/users").text)
        again = client.post(
            "/users",
            data={
                "csrf_token": token,
                "username": "casey",
                "password": MEMBER_PASSWORD,
                "password2": MEMBER_PASSWORD,
                "role": "member",
            },
            headers=ORIGIN,
        )
        assert again.status_code == 400
        assert "already in use" in again.text
        client.post(
            "/logout",
            data={"csrf_token": csrf_from(client.get("/").text)},
            headers=SAME_SITE,
            follow_redirects=False,
        )
        assert login(client, username="ADMIN", password=PASSWORD).status_code == 303
        client.post(
            "/logout",
            data={"csrf_token": csrf_from(client.get("/").text)},
            headers=SAME_SITE,
            follow_redirects=False,
        )
        assert login(client, username="cAsEy", password=MEMBER_PASSWORD).status_code == 303


def test_reset_admin_keeps_member_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    bundle = build(tmp_path)
    _member(bundle)
    with TestClient(bundle.app) as client:
        sign_in(client)
    from skylight_wifi_automation.main import main

    main(["reset-admin"])
    names = {row["username"] for row in db_rows(bundle.store.path, "SELECT username, role FROM users")}
    assert names == {MEMBER}
    with TestClient(bundle.app) as client:
        assert client.get("/setup", follow_redirects=False).status_code == 200


def _v1_database(path, created: datetime, token: str, *, last_seen: datetime) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version(version) VALUES (1);
        CREATE TABLE admin (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            failed_count INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT
        );
        CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY,
            csrf TEXT NOT NULL,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE TABLE login_attempts (
            id INTEGER PRIMARY KEY,
            ip TEXT NOT NULL,
            username TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            lock_kind TEXT,
            locked_until TEXT
        );
        CREATE TABLE form_tokens (token_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE children (
            id INTEGER PRIMARY KEY,
            label TEXT NOT NULL UNIQUE,
            skylight_profile TEXT NOT NULL UNIQUE,
            object_name TEXT NOT NULL UNIQUE,
            cutoff TEXT NOT NULL
        );
        CREATE TABLE secrets (
            name TEXT PRIMARY KEY,
            nonce BLOB NOT NULL,
            ciphertext BLOB NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE overrides (
            id INTEGER PRIMARY KEY,
            child_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            set_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            cleared_at TEXT,
            cleared_by TEXT,
            clear_reason TEXT
        );
        CREATE TABLE audit (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            diff TEXT NOT NULL
        );
        CREATE TABLE config_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        );
        INSERT INTO config_version(id, version) VALUES (1, 0);
        """
    )
    connection.execute(
        "INSERT INTO admin(username, password_hash, failed_count) VALUES ('Admin', '$argon2id$kept', 0)"
    )
    connection.execute(
        """INSERT INTO sessions(token_hash, csrf, username, created_at, expires_at, last_seen)
           VALUES (?, 'csrf-value', 'Admin', ?, ?, ?)""",
        (
            hash_token(token),
            dump_dt(created),
            dump_dt(created + timedelta(days=7)),
            dump_dt(last_seen),
        ),
    )
    connection.commit()
    connection.close()


def test_v1_session_keeps_its_seven_day_lifetime(tmp_path):
    moment = datetime(2026, 1, 15, 12, tzinfo=UTC)
    created = moment - timedelta(hours=20)
    token = "legacy-session-token"
    _v1_database(tmp_path / "app.db", created, token, last_seen=moment - timedelta(minutes=30))
    previous = os.umask(0)
    try:
        bundle = build(tmp_path, moment=moment)
    finally:
        os.umask(previous)
    path = bundle.store.path
    backups = list(path.parent.glob("app.db.bak-*"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    row = db_rows(path, "SELECT legacy, cap_at FROM sessions")[0]
    assert row["legacy"] == 1
    assert row["cap_at"] == dump_dt(created)

    def opened(client: TestClient):
        client.cookies.clear()
        return client.get(
            "/",
            headers={"cookie": f"{PLAIN_SESSION_COOKIE}={token}"},
            follow_redirects=False,
        )

    with TestClient(bundle.app) as client:
        first = opened(client)
        assert first.status_code == 200
        assert "set-cookie" not in {name.lower() for name in first.headers}
        deadline = created + timedelta(days=7) - timedelta(minutes=1)
        while bundle.clock.moment + timedelta(hours=11) < deadline:
            bundle.clock.moment += timedelta(hours=11)
            stepped = opened(client)
            assert stepped.status_code == 200
            assert "set-cookie" not in {name.lower() for name in stepped.headers}
        bundle.clock.moment = deadline
        assert opened(client).status_code == 200
        bundle.clock.moment = created + timedelta(days=7) + timedelta(seconds=1)
        assert opened(client).status_code == 303


def test_migration_refuses_case_colliding_admin_names(tmp_path):
    path = tmp_path / "app.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version(version) VALUES (1);
        CREATE TABLE admin (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            failed_count INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT
        );
        INSERT INTO admin(username, password_hash) VALUES ('Admin', '$argon2id$kept');
        INSERT INTO admin(username, password_hash) VALUES ('admin', '$argon2id$other');
        CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY,
            csrf TEXT NOT NULL,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()
    try:
        Store(path, None)
    except RuntimeError as exc:
        assert "collide" in str(exc)
    else:
        raise AssertionError("migration should have refused the colliding names")
    names = {row["username"] for row in db_rows(path, "SELECT username FROM admin")}
    assert names == {"Admin", "admin"}
    assert db_rows(path, "SELECT MAX(version) AS version FROM schema_version")[0]["version"] == 1
    assert db_rows(path, "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'users'") == []


def test_setup_rejects_a_case_colliding_username(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    bundle = build(tmp_path)
    asyncio.run(
        bundle.store.create_user("setup", bundle.clock(), "Casey", FAST.hash(MEMBER_PASSWORD), "member")
    )
    with TestClient(bundle.app) as client:
        page = client.get("/setup")
        posted = client.post(
            "/setup",
            data={
                "csrf_token": csrf_from(page.text),
                "setup_token": SETUP_TOKEN,
                "username": "CASEY",
                "password": PASSWORD,
                "password2": PASSWORD,
            },
            headers=SAME_SITE,
        )
    assert posted.status_code == 400
    assert "already in use" in posted.text
    assert {row["username"] for row in db_rows(bundle.store.path, "SELECT username FROM users")} == {"Casey"}


def test_password_change_keeps_the_original_absolute_cap(tmp_path):
    start = datetime(2026, 1, 15, tzinfo=UTC)
    bundle = build(tmp_path, moment=start)
    new_password = "rotated-password"
    with TestClient(bundle.app) as client:
        sign_in(client)
        client.cookies.clear()
        page = client.get("/login")
        signed = client.post(
            "/login",
            data={
                "csrf_token": csrf_from(page.text),
                "username": USERNAME,
                "password": PASSWORD,
                "remember": "1",
            },
            headers=SAME_SITE,
            follow_redirects=False,
        )
        value = _cookie_value(signed.headers["set-cookie"])
        for _days in (25, 25, 20):
            bundle.clock.moment += timedelta(days=_days)
            client.cookies.clear()
            slid = client.get(
                "/",
                headers={"cookie": f"{PLAIN_SESSION_COOKIE}={value}"},
                follow_redirects=False,
            )
            assert slid.status_code == 200
        client.cookies.clear()
        form = client.get("/password", headers={"cookie": f"{PLAIN_SESSION_COOKIE}={value}"})
        changed = client.post(
            "/password",
            data={
                "csrf_token": csrf_from(form.text),
                "current_password": PASSWORD,
                "new_password": new_password,
                "password2": new_password,
            },
            headers={**ORIGIN, "cookie": f"{PLAIN_SESSION_COOKIE}={value}"},
            follow_redirects=False,
        )
        assert changed.status_code == 303
        fresh = changed.headers["set-cookie"]
        new_value = _cookie_value(fresh)
        assert new_value != value
        assert "max-age=1728000" in fresh.lower()
        client.cookies.clear()
        assert (
            client.get(
                "/",
                headers={"cookie": f"{PLAIN_SESSION_COOKIE}={new_value}"},
                follow_redirects=False,
            ).status_code
            == 200
        )
        bundle.clock.moment = start + timedelta(days=89)
        client.cookies.clear()
        assert (
            client.get(
                "/",
                headers={"cookie": f"{PLAIN_SESSION_COOKIE}={new_value}"},
                follow_redirects=False,
            ).status_code
            == 200
        )
        bundle.clock.moment = start + timedelta(days=90, seconds=1)
        client.cookies.clear()
        assert (
            client.get(
                "/",
                headers={"cookie": f"{PLAIN_SESSION_COOKIE}={new_value}"},
                follow_redirects=False,
            ).status_code
            == 303
        )


def test_rejection_log_cannot_be_split_by_a_newline(tmp_path, caplog):
    from types import SimpleNamespace

    from skylight_wifi_automation.web import _log_rejection

    caplog.set_level(logging.WARNING, logger="skylight_wifi_automation.web")
    # %0d%0a in the request target is this decoded path.
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/secrets/x\r\nEVIL/clear"),
        headers={"origin": "https://evil.example"},
    )
    _log_rejection(request, "origin_mismatch")
    records = [record for record in caplog.records if "rejected state-changing" in record.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "\n" not in message
    assert "\r" not in message
    assert "%0D%0A" in message
    assert "EVIL" in message
