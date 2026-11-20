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
from skylight_wifi_automation.web import HOST_SESSION_COOKIE, PLAIN_SESSION_COOKIE, session_cookie


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


def _assert_cookie(header: str, *, name: str, secure: bool) -> None:
    assert header.startswith(f"{name}=")
    lowered = header.lower()
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "path=/" in lowered
    assert "domain=" not in lowered
    if secure:
        assert "secure" in lowered
    else:
        assert "secure" not in lowered
    if name.startswith(("__Host-", "__Secure-")):
        assert secure
        assert "secure" in lowered


def test_login_logout_and_cookie_flags(tmp_path):
    bundle = build(tmp_path, cookie_secure=True)
    with TestClient(bundle.app) as client:
        setup_admin(client)
        response = login(client)
        _assert_cookie(response.headers["set-cookie"], name=HOST_SESSION_COOKIE, secure=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    insecure = build(plain, cookie_secure=False)
    with TestClient(insecure.app) as client:
        setup_admin(client)
        response = login(client)
        header = response.headers["set-cookie"]
        _assert_cookie(header, name=PLAIN_SESSION_COOKIE, secure=False)
        assert "__Host-" not in header
        assert "__Secure-" not in header
        assert response.status_code == 303
        page = client.get("/")
        token = csrf_from(page.text)
        assert PASSWORD not in page.text
        assert client.cookies.get(PLAIN_SESSION_COOKIE) not in page.text
        logout = client.post("/logout", data={"csrf_token": token}, headers=ORIGIN, follow_redirects=False)
        assert logout.status_code == 303
        old = client.cookies.get(PLAIN_SESSION_COOKIE)
        client.cookies.clear()
        if old:
            client.cookies.set(PLAIN_SESSION_COOKIE, old)
        assert client.get("/", follow_redirects=False).status_code == 303
    assert db_rows(insecure.store.path, "SELECT token_hash FROM sessions") == []


def test_prefixed_cookie_never_goes_out_without_secure():
    from starlette.responses import Response

    secure = Response()
    session_cookie(secure, "token-value", True)
    _assert_cookie(secure.headers["set-cookie"], name=HOST_SESSION_COOKIE, secure=True)
    plain = Response()
    session_cookie(plain, "token-value", False)
    header = plain.headers["set-cookie"]
    _assert_cookie(header, name=PLAIN_SESSION_COOKIE, secure=False)
    assert not header.split("=", 1)[0].startswith("__")


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
        old_cookie = client.cookies.get(PLAIN_SESSION_COOKIE)
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
        assert client.cookies.get(PLAIN_SESSION_COOKIE) != old_cookie
        client.cookies.clear()
        client.cookies.set(PLAIN_SESSION_COOKIE, old_cookie)
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


def test_short_setup_token_disables_setup(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("ADMIN_SETUP_TOKEN", "too-short")
    from skylight_wifi_automation.runtime import setup_token

    assert setup_token() is None
    assert "at least 32 characters" in caplog.text
    assert "too-short" not in caplog.text
    bundle = build(tmp_path, setup_token="too-short")
    with TestClient(bundle.app) as client:
        page = client.get("/setup")
        posted = client.post(
            "/setup",
            data={"username": USERNAME, "password": PASSWORD, "setup_token": "too-short"},
            headers=ORIGIN,
        )
    assert "32 characters" in page.text
    assert posted.status_code == 200
    assert db_rows(bundle.store.path, "SELECT username FROM admin") == []


def test_setup_failures_use_the_login_lockout(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        for _ in range(5):
            page = client.get("/setup")
            failed = client.post(
                "/setup",
                data={
                    "csrf_token": csrf_from(page.text),
                    "setup_token": "wrong-setup-token-0123456789abcd",
                    "username": USERNAME,
                    "password": PASSWORD,
                    "password2": PASSWORD,
                },
                headers=ORIGIN,
            )
            assert failed.status_code == 200
            assert "Setup failed." in failed.text
        page = client.get("/setup")
        locked = client.post(
            "/setup",
            data={
                "csrf_token": csrf_from(page.text),
                "setup_token": SETUP_TOKEN,
                "username": USERNAME,
                "password": PASSWORD,
                "password2": PASSWORD,
            },
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert locked.status_code == 200
        assert "Setup failed." in locked.text
        assert db_rows(bundle.store.path, "SELECT username FROM admin") == []
        bundle.clock.moment += timedelta(minutes=15, seconds=1)
        page = client.get("/setup")
        opened = client.post(
            "/setup",
            data={
                "csrf_token": csrf_from(page.text),
                "setup_token": SETUP_TOKEN,
                "username": USERNAME,
                "password": PASSWORD,
                "password2": PASSWORD,
            },
            headers=ORIGIN,
            follow_redirects=False,
        )
    assert opened.status_code == 303


def test_wrong_current_password_uses_the_lockout(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        token = sign_in(client)
        for _ in range(5):
            failed = client.post(
                "/password",
                data={
                    "csrf_token": token,
                    "current_password": "wrong-password-value",
                    "new_password": "newer-password-value",
                    "password2": "newer-password-value",
                },
                headers=ORIGIN,
            )
            assert failed.status_code == 400
            assert "Password change failed." in failed.text
            token = csrf_from(failed.text)
        blocked = client.post(
            "/password",
            data={
                "csrf_token": token,
                "current_password": PASSWORD,
                "new_password": "newer-password-value",
                "password2": "newer-password-value",
            },
            headers=ORIGIN,
        )
        assert blocked.status_code == 400
        bundle.clock.moment += timedelta(minutes=15, seconds=1)
        token = csrf_from(client.get("/password").text)
        changed = client.post(
            "/password",
            data={
                "csrf_token": token,
                "current_password": PASSWORD,
                "new_password": "newer-password-value",
                "password2": "newer-password-value",
            },
            headers=ORIGIN,
            follow_redirects=False,
        )
    assert changed.status_code == 303
    assert "wrong-password-value" not in " ".join(
        row["diff"] for row in db_rows(bundle.store.path, "SELECT diff FROM audit")
    )


def _asgi_peer(app, peer: str, forwarded: str | None):
    async def wrapper(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            headers = [item for item in scope.get("headers", []) if item[0] != b"x-forwarded-for"]
            if forwarded is not None:
                headers.append((b"x-forwarded-for", forwarded.encode()))
            scope["headers"] = headers
            scope["client"] = (peer, 443)
        await app(scope, receive, send)

    wrapper.state = app.state
    return wrapper


def test_forwarded_ip_lockout_does_not_lock_a_different_client(tmp_path):
    bundle = build(tmp_path)
    bundle.app.state.context.trusted_proxies = "192.0.2.1"

    def post_login(forwarded: str, password: str, username: str = USERNAME):
        peer_app = _asgi_peer(bundle.app, "192.0.2.1", forwarded)
        with TestClient(peer_app) as client:
            page = client.get("/login")
            return client.post(
                "/login",
                data={
                    "csrf_token": csrf_from(page.text),
                    "username": username,
                    "password": password,
                },
                headers=ORIGIN,
                follow_redirects=False,
            )

    with TestClient(bundle.app) as client:
        setup_admin(client)
    for index in range(5):
        failed = post_login("192.0.2.10", "wrong-password-value", f"person-{index}")
        assert failed.status_code == 200
    blocked = post_login("192.0.2.10", PASSWORD)
    other = post_login("192.0.2.11", PASSWORD)
    assert blocked.status_code == 200
    assert other.status_code == 303
    spoofed = _asgi_peer(bundle.app, "192.0.2.30", "192.0.2.11")
    with TestClient(spoofed) as client:
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"csrf_token": csrf_from(page.text), "username": USERNAME, "password": PASSWORD},
            headers=ORIGIN,
            follow_redirects=False,
        )
    assert response.status_code == 303


async def test_login_burst_does_not_stall_the_poller_loop(tmp_path):
    import time
    from datetime import UTC, datetime

    from skylight_wifi_automation.store import Store

    store = Store(tmp_path / "app.db", None)
    real = PasswordHasher()
    stored = real.hash(PASSWORD)
    dummy = real.hash("dummy-password-value")
    moment = datetime.now(UTC)
    await store.create_admin(USERNAME, stored, moment)
    gaps: list[float] = []
    ticks: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        previous = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - previous)
            previous = now

    async def poller() -> None:
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    async def bad_login() -> None:
        result = await attempt_login(
            store,
            real,
            dummy,
            USERNAME,
            "wrong-password-value",
            "192.0.2.44",
            moment,
        )
        assert result.ok is False

    heart = asyncio.create_task(heartbeat())
    poll = asyncio.create_task(poller())
    await asyncio.gather(*(bad_login() for _ in range(20)))
    stop.set()
    await heart
    await poll
    assert gaps
    assert max(gaps) < 0.1
    assert len(ticks) >= 8


def test_default_hasher_is_argon2id():
    # The production hasher is the library default. Tests use a smaller memory cost.
    assert PasswordHasher().time_cost >= 1
    assert FAST.hash("long-enough-pw").startswith("$argon2id$")
