from __future__ import annotations

import asyncio
import re
from datetime import timedelta

from argon2 import PasswordHasher
from starlette.routing import Mount
from starlette.testclient import TestClient
from tests.uiutil import (
    FAST,
    ORIGIN,
    PASSWORD,
    SETUP_TOKEN,
    USERNAME,
    build,
    csrf_from,
    db_rows,
    login,
    setup_admin,
    sign_in,
)

from skylight_wifi_automation.auth import attempt_login, dummy_password_hash
from skylight_wifi_automation.main import main, parse_args
from skylight_wifi_automation.timeutil import dump_dt
from skylight_wifi_automation.web import SESSION_COOKIE


def test_setup_is_disabled_without_a_token(tmp_path):
    bundle = build(tmp_path, setup_token=None)
    with TestClient(bundle.app) as client:
        page = client.get("/setup")
        posted = client.post("/setup", data={"username": USERNAME, "password": PASSWORD}, headers=ORIGIN)
    assert page.status_code == 200
    assert "ADMIN_SETUP_TOKEN" in page.text
    assert "no default password" in page.text.lower()
    assert posted.status_code == 200
    assert db_rows(bundle.store.path, "SELECT username FROM admin") == []


def test_setup_rejects_a_bad_token_and_a_short_password(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        page = client.get("/setup")
        token = csrf_from(page.text)
        bad = client.post(
            "/setup",
            data={
                "csrf_token": token,
                "setup_token": "wrong-token-value",
                "username": USERNAME,
                "password": PASSWORD,
                "password2": PASSWORD,
            },
            headers=ORIGIN,
        )
        page = client.get("/setup")
        short = client.post(
            "/setup",
            data={
                "csrf_token": csrf_from(page.text),
                "setup_token": SETUP_TOKEN,
                "username": USERNAME,
                "password": "short-pass",
                "password2": "short-pass",
            },
            headers=ORIGIN,
        )
    assert bad.status_code == 200
    assert "wrong-token-value" not in bad.text
    assert PASSWORD not in bad.text
    assert short.status_code == 400
    assert db_rows(bundle.store.path, "SELECT username FROM admin") == []
    audit = " ".join(row["diff"] + row["action"] for row in db_rows(bundle.store.path, "SELECT * FROM audit"))
    assert PASSWORD not in audit
    assert "wrong-token-value" not in audit


def test_setup_creates_one_admin_then_returns_404(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        created = setup_admin(client)
        assert created.status_code == 303
        assert client.get("/setup").status_code == 404
        assert client.post("/setup", data={"setup_token": SETUP_TOKEN}, headers=ORIGIN).status_code == 404
    rows = db_rows(bundle.store.path, "SELECT password_hash FROM admin")
    assert len(rows) == 1
    assert rows[0]["password_hash"].startswith("$argon2id$")


def test_login_logout_and_cookie_flags(tmp_path):
    bundle = build(tmp_path, cookie_secure=True)
    with TestClient(bundle.app) as client:
        setup_admin(client)
        response = login(client)
        cookie = response.headers["set-cookie"]
        lowered = cookie.lower()
        assert SESSION_COOKIE in cookie
        assert "httponly" in lowered
        assert "samesite=strict" in lowered
        assert "path=/" in lowered
        assert "secure" in lowered
        assert "domain=" not in lowered
    plain = tmp_path / "plain"
    plain.mkdir()
    insecure = build(plain, cookie_secure=False)
    with TestClient(insecure.app) as client:
        token = sign_in(client)
        assert PASSWORD not in client.get("/").text
        page = client.get("/")
        assert client.cookies.get(SESSION_COOKIE) not in page.text
        logout = client.post("/logout", data={"csrf_token": token}, headers=ORIGIN, follow_redirects=False)
        assert logout.status_code == 303
        old = client.cookies.get(SESSION_COOKIE)
        client.cookies.clear()
        if old:
            client.cookies.set(SESSION_COOKIE, old)
        assert client.get("/", follow_redirects=False).status_code == 303
    assert db_rows(insecure.store.path, "SELECT token_hash FROM sessions") == []


def test_login_failure_is_generic_and_locks_out(tmp_path):
    bundle = build(tmp_path)
    wrong = "wrong-password-value"
    with TestClient(bundle.app) as client:
        setup_admin(client)
        for _ in range(5):
            failed = login(client, password=wrong)
            assert failed.status_code == 200
            assert "Sign-in failed." in failed.text
            assert wrong not in failed.text
        locked = login(client, password=PASSWORD)
        assert locked.status_code == 200
        assert "Sign-in failed." in locked.text
        assert "lock" not in locked.text.lower()
        bundle.clock.moment += timedelta(minutes=15, seconds=1)
        opened = login(client, password=PASSWORD)
        assert opened.status_code == 303
    audit = "\n".join(
        f"{row['action']} {row['diff']}" for row in db_rows(bundle.store.path, "SELECT action, diff FROM audit")
    )
    assert wrong not in audit
    assert PASSWORD not in audit
    assert "lockout" in audit
    assert "login_failure" in audit


def test_ip_lockout_does_not_block_a_different_address(tmp_path):
    bundle = build(tmp_path)

    async def scenario() -> None:
        await bundle.store.create_admin(
            USERNAME, FAST.hash(PASSWORD), bundle.clock()
        )
        dummy = dummy_password_hash(FAST)
        for index in range(5):
            result = await attempt_login(
                bundle.store,
                FAST,
                dummy,
                f"person-{index}",
                "wrong-password-value",
                "192.0.2.8",
                bundle.clock(),
            )
        assert result.locked is True
        blocked = await attempt_login(
            bundle.store, FAST, dummy, USERNAME, PASSWORD, "192.0.2.8", bundle.clock()
        )
        allowed = await attempt_login(
            bundle.store, FAST, dummy, USERNAME, PASSWORD, "192.0.2.9", bundle.clock()
        )
        assert blocked.ok is False and blocked.locked is True
        assert allowed.ok is True

    asyncio.run(scenario())


def test_session_idle_and_absolute_expiry(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        sign_in(client)
        connection = __import__("sqlite3").connect(bundle.store.path)
        connection.execute(
            "UPDATE sessions SET last_seen = ?",
            (dump_dt(bundle.clock.moment - timedelta(hours=12, seconds=1)),),
        )
        connection.commit()
        connection.close()
        assert client.get("/", follow_redirects=False).status_code == 303
        assert login(client).status_code == 303
        connection = __import__("sqlite3").connect(bundle.store.path)
        connection.execute(
            "UPDATE sessions SET expires_at = ?",
            (dump_dt(bundle.clock.moment - timedelta(seconds=1)),),
        )
        connection.commit()
        connection.close()
        assert client.get("/", follow_redirects=False).status_code == 303


def test_password_change_rotates_the_session(tmp_path):
    bundle = build(tmp_path)
    new_password = "newer-password-value"
    with TestClient(bundle.app) as client:
        token = sign_in(client)
        old_cookie = client.cookies.get(SESSION_COOKIE)
        changed = client.post(
            "/password",
            data={
                "csrf_token": token,
                "current_password": PASSWORD,
                "new_password": new_password,
                "password2": new_password,
            },
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert changed.status_code == 303
        assert client.cookies.get(SESSION_COOKIE) != old_cookie
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, old_cookie)
        assert client.get("/", follow_redirects=False).status_code == 303
        assert login(client, password=PASSWORD).status_code == 200
        assert login(client, password=new_password).status_code == 303
    audit = " ".join(row["diff"] for row in db_rows(bundle.store.path, "SELECT diff FROM audit"))
    assert new_password not in audit
    assert PASSWORD not in audit


def test_every_non_public_route_requires_a_session(tmp_path):
    bundle = build(tmp_path)
    public_get = {"/healthz", "/login", "/setup"}
    public_post = {"/login", "/setup"}
    with TestClient(bundle.app) as client:
        assert client.get("/healthz").status_code == 200
        for route in bundle.app.routes:
            if isinstance(route, Mount):
                continue
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            for method in route.methods or set():
                if method in {"HEAD", "OPTIONS"}:
                    continue
                if method == "GET" and path in public_get:
                    continue
                if method == "POST" and path in public_post:
                    continue
                response = client.request(method, path, follow_redirects=False)
                assert response.status_code in {401, 303}, (method, path, response.status_code)
        assert client.get("/not-a-page", follow_redirects=False).status_code == 303
        assert client.post("/not-a-page", follow_redirects=False).status_code == 401


def test_reset_admin_cli_allows_setup_again(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        assert setup_admin(client).status_code == 303
    assert parse_args(["reset-admin"]).command == "reset-admin"
    main(["reset-admin"])
    with TestClient(bundle.app) as client:
        page = client.get("/setup")
    assert page.status_code == 200
    assert "Create the admin" in page.text
    assert db_rows(bundle.store.path, "SELECT username FROM admin") == []
    assert db_rows(bundle.store.path, "SELECT token_hash FROM sessions") == []


def test_default_hasher_is_argon2id():
    # The production hasher is the library default. Tests use a smaller memory cost.
    assert PasswordHasher().time_cost >= 1
    assert FAST.hash("long-enough-pw").startswith("$argon2id$")
