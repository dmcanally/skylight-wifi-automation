from __future__ import annotations

import dataclasses

import pytest
from starlette.testclient import TestClient
from tests.uiutil import ORIGIN, build, csrf_from, db_rows

from skylight_wifi_automation.runtime import auth_disabled
from skylight_wifi_automation.web import create_app


def open_app(tmp_path):
    bundle = build(tmp_path)
    context = dataclasses.replace(bundle.app.state.context, auth_disabled=True)
    return bundle, create_app(context)


def test_default_keeps_sign_in(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        response = client.get("/", follow_redirects=False)
    assert response.status_code == 303


def test_dashboard_opens_without_a_session(tmp_path):
    _bundle, app = open_app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/", follow_redirects=False)
        settings = client.get("/settings", follow_redirects=False)
    assert page.status_code == 200
    assert "Sign-in is turned off" in page.text
    assert "Log out" not in page.text
    assert settings.status_code == 200


def test_account_pages_are_gone(tmp_path):
    _bundle, app = open_app(tmp_path)
    with TestClient(app) as client:
        login = client.get("/login", follow_redirects=False)
        setup = client.get("/setup", follow_redirects=False)
        users = client.get("/users", follow_redirects=False)
        password = client.get("/password", follow_redirects=False)
        sessions = client.get("/sessions", follow_redirects=False)
        health = client.get("/healthz")
    assert login.status_code == 303 and login.headers["location"] == "/"
    assert setup.status_code == 303 and setup.headers["location"] == "/"
    assert users.status_code == 404
    assert password.status_code == 404
    assert sessions.status_code == 404
    assert health.status_code == 200


def test_forms_still_need_csrf_and_same_origin(tmp_path):
    bundle, app = open_app(tmp_path)
    with TestClient(app) as client:
        token = csrf_from(client.get("/settings").text)
        missing = client.post(
            "/settings",
            data={"timezone": "UTC", "poll_seconds": "60"},
            headers=ORIGIN,
            follow_redirects=False,
        )
        cross = client.post(
            "/settings",
            data={"csrf_token": token, "timezone": "UTC", "poll_seconds": "60"},
            headers={"origin": "http://evil.example"},
            follow_redirects=False,
        )
    assert missing.status_code == 403
    assert cross.status_code == 403
    rows = db_rows(bundle.store.path, "SELECT value FROM settings WHERE key = 'timezone'")
    assert rows[0]["value"] == '"America/Chicago"'


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, False), ("", False), ("false", False), ("0", False), ("true", True), ("1", True)],
)
def test_flag_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("AUTH_DISABLED", raising=False)
    else:
        monkeypatch.setenv("AUTH_DISABLED", raw)
    assert auth_disabled() is expected


def test_flag_rejects_garbage(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "maybe")
    with pytest.raises(RuntimeError):
        auth_disabled()


def test_unblock_and_clear_work_without_a_session(tmp_path):
    bundle, app = open_app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/")
        child_id = db_rows(bundle.store.path, "SELECT id FROM children")[0]["id"]
        unblock = client.post(
            f"/overrides/{child_id}",
            data={"csrf_token": csrf_from(page.text), "action": "unblock"},
            headers=ORIGIN,
            follow_redirects=False,
        )
        active = db_rows(bundle.store.path, "SELECT set_by FROM overrides WHERE cleared_at IS NULL")
        clear = client.post(
            f"/overrides/{child_id}/clear",
            data={"csrf_token": csrf_from(page.text)},
            headers=ORIGIN,
            follow_redirects=False,
        )
    assert unblock.status_code == 303
    assert [row["set_by"] for row in active] == ["anonymous"]
    assert clear.status_code == 303
    assert db_rows(bundle.store.path, "SELECT id FROM overrides WHERE cleared_at IS NULL") == []
