from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

from starlette.testclient import TestClient
from tests.uiutil import (
    FAST,
    ORIGIN,
    PASSWORD,
    SAME_SITE,
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

    return re.findall(r'action="/sessions/(\d+)/revoke"', html)


def _current(html: str) -> str:
    import re

    match = re.search(r'data-current="yes".*?action="/sessions/(\d+)/revoke"', html, re.DOTALL)
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
        assert denied.status_code == 403
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
        INSERT INTO admin(username, password_hash, failed_count) VALUES ('admin', '$argon2id$kept', 2);
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
           VALUES (?, 'csrf-value', 'admin', ?, ?, ?)""",
        (hash_token(token), dump_dt(now), dump_dt(now + timedelta(days=3)), dump_dt(now)),
    )
    connection.commit()
    connection.close()
    store = Store(path, None)
    try:
        account = asyncio.run(store.get_user("admin"))
        assert account is not None
        assert account.role == "admin"
        assert account.password_hash == "$argon2id$kept"
        assert account.failed_count == 2
        session = asyncio.run(store.use_session(token, now))
        assert session is not None and session.username == "admin"
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
