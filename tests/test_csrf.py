from __future__ import annotations

from starlette.testclient import TestClient
from tests.uiutil import ORIGIN, PASSWORD, build, csrf_from, db_rows, sign_in


def test_missing_or_invalid_csrf_is_rejected(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        sign_in(client)
        missing = client.post(
            "/settings",
            data={"timezone": "UTC", "poll_seconds": "60"},
            headers=ORIGIN,
            follow_redirects=False,
        )
        bad = client.post(
            "/settings",
            data={"csrf_token": "not-the-session-token", "timezone": "UTC", "poll_seconds": "60"},
            headers=ORIGIN,
            follow_redirects=False,
        )
        login_page = client.get("/login")
    assert missing.status_code == 403
    assert bad.status_code == 403
    assert "Cross-site request blocked." in missing.text
    # Logging out was not the point; the saved timezone must stay Chicago.
    assert db_rows(bundle.store.path, "SELECT value FROM settings WHERE key = 'timezone'")[0]["value"] == '"America/Chicago"'
    # A fresh login form token cannot be replayed.
    token = csrf_from(login_page.text)
    with TestClient(bundle.app) as client:
        first = client.post(
            "/login",
            data={"csrf_token": token, "username": "admin", "password": PASSWORD},
            headers=ORIGIN,
            follow_redirects=False,
        )
        second = client.post(
            "/login",
            data={"csrf_token": token, "username": "admin", "password": PASSWORD},
            headers=ORIGIN,
            follow_redirects=False,
        )
    assert first.status_code == 303
    assert second.status_code == 403


def test_cross_origin_is_rejected_and_referer_can_stand_in(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        token = sign_in(client)
        evil = client.post(
            "/settings",
            data={"csrf_token": token, "timezone": "UTC", "poll_seconds": "30"},
            headers={"origin": "https://evil.example"},
            follow_redirects=False,
        )
        neither = client.post(
            "/settings",
            data={"csrf_token": token, "timezone": "UTC", "poll_seconds": "30"},
            follow_redirects=False,
        )
        referred = client.post(
            "/settings",
            data={
                "csrf_token": token,
                "timezone": "America/Chicago",
                "poll_seconds": "60",
                "dry_run": "false",
                "include_late": "true",
                "include_routines": "true",
                "empty_day_unlocks": "false",
            },
            headers={"referer": "http://testserver/settings"},
            follow_redirects=False,
        )
    assert evil.status_code == 403
    assert neither.status_code == 403
    assert referred.status_code == 303
    assert "evil.example" not in evil.text
