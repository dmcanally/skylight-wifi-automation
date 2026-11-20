from __future__ import annotations

import logging

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


def test_rejection_names_the_check_and_hides_secrets(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="skylight_wifi_automation.web")
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        token = sign_in(client)
        cookie = client.cookies.get("swa_session")
        missing = client.post("/settings", data={"timezone": "UTC"}, headers=ORIGIN, follow_redirects=False)
        mismatch = client.post(
            "/settings",
            data={
                "csrf_token": "not-the-session-token",
                "timezone": "UTC",
                "poll_seconds": "60",
                "dry_run": "false",
                "include_late": "true",
                "include_routines": "true",
                "empty_day_unlocks": "false",
            },
            headers=ORIGIN,
            follow_redirects=False,
        )
        cross = client.post(
            **_settings(token, **{"origin": "null", "sec-fetch-site": "cross-site"}),
        )
        silent = client.post(**_settings(token))
        fresh = csrf_from(mismatch.text)
        saved = client.post(**_settings(fresh, **ORIGIN))
    assert missing.status_code == 403
    assert "Cross-site request blocked." in missing.text
    assert mismatch.status_code == 403
    assert "Your session changed, please try again." in mismatch.text
    assert saved.status_code == 303
    assert cross.status_code == 403
    assert "Your session changed, please try again." not in cross.text
    assert silent.status_code == 403
    text = caplog.text
    assert "check=csrf_mismatch" in text
    assert "check=fetch_site" in text
    assert "check=no_signal" in text
    assert "check=form_token" in text or "check=csrf_mismatch" in text
    assert "not-the-session-token" not in text
    assert token not in text
    assert PASSWORD not in text
    assert cookie not in text
    assert "sec_fetch_site=cross-site" in text
    assert "origin_present=yes" in text
    assert db_rows(bundle.store.path, "SELECT value FROM settings WHERE key = 'timezone'")[0]["value"] == '"America/Chicago"'


def test_plain_http_nodeport_headers_allow_the_first_post(tmp_path):
    """Chrome and WebKit on plain HTTP send Origin with the non-default port.

    Measured with headless Chrome 157 and Playwright WebKit 27.2 (Safari 27.2)
    against http://192.0.2.10:30080. Both sent Origin and Referer including the
    port, and omitted Sec-Fetch-Site because the address is not a trustworthy
    URL. Loopback is trustworthy, so both sent Sec-Fetch-Site: same-origin.
    Safari before 16.4 has no Sec-Fetch-* headers; WebKit has sent Origin on
    form POST since 2008, so the Origin comparison covers those versions. A
    client that omits Origin entirely still passes when the Referer host matches.
    An Origin that drops the port does not match and stays a 403.
    """
    bundle = build(tmp_path)
    chrome = {
        "origin": "http://lan.example:30080",
        "referer": "http://lan.example:30080/settings",
    }
    with TestClient(bundle.app, base_url="http://lan.example:30080") as client:
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
            headers=chrome,
            follow_redirects=False,
        )
        assert created.status_code == 303
        page = client.get("/login")
        signed = client.post(
            "/login",
            data={"csrf_token": csrf_from(page.text), "username": USERNAME, "password": PASSWORD},
            headers={**chrome, "referer": "http://lan.example:30080/login"},
            follow_redirects=False,
        )
        assert signed.status_code == 303
        home = client.get("/")
        assert home.status_code == 200
        saved = client.post(**_settings(csrf_from(home.text), **chrome))
        assert saved.status_code == 303
        referred = client.post(
            **_settings(
                csrf_from(client.get("/").text),
                referer="http://lan.example:30080/settings",
            )
        )
        assert referred.status_code == 303
        stripped = client.post(
            **_settings(csrf_from(client.get("/").text), origin="http://lan.example")
        )
    assert stripped.status_code == 403
    assert "Cross-site request blocked." in stripped.text

    loop_dir = tmp_path / "loop"
    loop_dir.mkdir()
    loopback = build(loop_dir)
    headers = {
        "origin": "http://127.0.0.1:30080",
        "sec-fetch-site": "same-origin",
        "referer": "http://127.0.0.1:30080/login",
    }
    with TestClient(loopback.app, base_url="http://127.0.0.1:30080") as client:
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
            headers=headers,
            follow_redirects=False,
        )
        assert created.status_code == 303
        page = client.get("/login")
        signed = client.post(
            "/login",
            data={"csrf_token": csrf_from(page.text), "username": USERNAME, "password": PASSWORD},
            headers=headers,
            follow_redirects=False,
        )
        assert signed.status_code == 303
        home = client.get("/")
        posted = client.post(
            "/overrides/1",
            data={"csrf_token": csrf_from(home.text), "action": "block"},
            headers=headers,
            follow_redirects=False,
        )
    assert posted.status_code == 303


def test_replayed_login_token_asks_the_user_to_retry(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="skylight_wifi_automation.web")
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        page = client.get("/setup")
        client.post(
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
        page = client.get("/login")
        token = csrf_from(page.text)
        first = client.post(
            "/login",
            data={"csrf_token": token, "username": USERNAME, "password": PASSWORD},
            headers=ORIGIN,
            follow_redirects=False,
        )
        second = client.post(
            "/login",
            data={"csrf_token": token, "username": USERNAME, "password": PASSWORD},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert first.status_code == 303
        assert second.status_code == 403
        assert "Your session changed, please try again." in second.text
        assert token not in caplog.text
        assert PASSWORD not in caplog.text
        assert SETUP_TOKEN not in caplog.text
        assert "check=form_token" in caplog.text
        retried = client.post(
            "/login",
            data={
                "csrf_token": csrf_from(second.text),
                "username": USERNAME,
                "password": PASSWORD,
            },
            headers=ORIGIN,
            follow_redirects=False,
        )
    assert retried.status_code == 303
