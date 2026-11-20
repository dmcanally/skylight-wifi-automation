from __future__ import annotations

from starlette.testclient import TestClient
from tests.uiutil import (
    ORIGIN,
    PASSWORD,
    SAME_SITE,
    SETUP_TOKEN,
    USERNAME,
    build,
    csrf_from,
    db_rows,
    sign_in,
)


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
    assert referred.headers["referrer-policy"] == "same-origin"


def _settings(token: str, **headers: str) -> dict:
    return {
        "url": "/settings",
        "data": {
            "csrf_token": token,
            "timezone": "America/Chicago",
            "poll_seconds": "60",
            "dry_run": "false",
            "include_late": "true",
            "include_routines": "true",
            "empty_day_unlocks": "false",
        },
        "headers": headers,
        "follow_redirects": False,
    }


def test_setup_login_and_dashboard_need_no_origin_header(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        page = client.get("/setup")
        created = client.post(
            "/setup",
            data={
                "csrf_token": csrf_from(page.text),
                "setup_token": SETUP_TOKEN,
                "username": USERNAME,
                "password": PASSWORD,
                "password2": PASSWORD,
            },
            headers=SAME_SITE,
            follow_redirects=False,
        )
        assert created.status_code == 303
        assert "origin" not in {name.lower() for name in SAME_SITE}
        page = client.get("/login")
        signed = client.post(
            "/login",
            data={"csrf_token": csrf_from(page.text), "username": USERNAME, "password": PASSWORD},
            headers=SAME_SITE,
            follow_redirects=False,
        )
        assert signed.status_code == 303
        home = client.get("/")
        assert home.status_code == 200
        assert "child-a" in home.text
        saved = client.post(**_settings(csrf_from(home.text), **SAME_SITE))
    assert saved.status_code == 303
    assert saved.headers["referrer-policy"] == "same-origin"
    assert db_rows(bundle.store.path, "SELECT value FROM settings WHERE key = 'timezone'")[0]["value"] == '"America/Chicago"'


def test_fetch_site_and_null_origin(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        token = sign_in(client)
        browser = client.post(
            **_settings(token, **{"sec-fetch-site": "same-origin"}),
        )
        null_browser = client.post(
            **_settings(token, **{"origin": "null", "sec-fetch-site": "same-origin"}),
        )
        null_only = client.post(**_settings(token, origin="null"))
        missing = client.post(**_settings(token))
        cross_site = client.post(
            **_settings(
                token,
                **{"origin": "null", "sec-fetch-site": "cross-site"},
            ),
        )
        cross_with_referer = client.post(
            **_settings(
                token,
                **{
                    "origin": "null",
                    "sec-fetch-site": "cross-site",
                    "referer": "http://testserver/settings",
                },
            ),
        )
        bad_origin = client.post(**_settings(token, origin="https://evil.example"))
        null_referer = client.post(
            **_settings(token, origin="null", referer="http://testserver/settings"),
        )
    assert browser.status_code == 303
    assert null_browser.status_code == 303
    assert null_only.status_code == 403
    assert missing.status_code == 403
    assert cross_site.status_code == 403
    assert cross_with_referer.status_code == 403
    assert bad_origin.status_code == 403
    assert null_referer.status_code == 303
