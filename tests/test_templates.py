from __future__ import annotations

from starlette.testclient import TestClient
from tests.uiutil import build, setup_admin, sign_in

LOGGED_IN_PAGES = ("/", "/children", "/settings", "/unifi", "/secrets", "/audit", "/password", "/login")


def _assert_document(html: str) -> None:
    lowered = html.lower()
    assert 'name="color-scheme" content="light dark"' in html
    assert "<script" not in lowered
    assert "<style" not in lowered
    assert "style=" not in lowered
    assert 'href="/static/app.css"' in html


def test_pages_render_without_inline_script_or_style(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        setup = client.get("/setup")
        disabled = None
    assert setup.status_code == 200
    _assert_document(setup.text)
    assert "Create the admin" in setup.text

    disabled_dir = tmp_path / "disabled"
    disabled_dir.mkdir()
    disabled_bundle = build(disabled_dir, setup_token=None)
    with TestClient(disabled_bundle.app) as client:
        disabled = client.get("/setup")
    assert disabled.status_code == 200
    _assert_document(disabled.text)
    assert "ADMIN_SETUP_TOKEN" in disabled.text

    with TestClient(bundle.app) as client:
        sign_in(client)
        for path in LOGGED_IN_PAGES:
            page = client.get(path)
            assert page.status_code == 200, path
            _assert_document(page.text)
        blocked = client.post(
            "/settings",
            data={"csrf_token": "present-but-wrong", "timezone": "UTC"},
            headers={"origin": "https://evil.example"},
            follow_redirects=False,
        )
    assert blocked.status_code == 403
    _assert_document(blocked.text)
    assert "Cross-site request blocked." in blocked.text
    assert "evil.example" not in blocked.text


def test_logged_out_pages_redirect_or_render(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        setup_admin(client)
        login = client.get("/login")
        home = client.get("/", follow_redirects=False)
        schedule = client.get("/children", follow_redirects=False)
        hidden = client.get("/setup", follow_redirects=False)
    assert login.status_code == 200
    _assert_document(login.text)
    assert "Sign in" in login.text
    assert home.status_code == 303
    assert home.headers["location"].endswith("/login")
    assert schedule.status_code == 303
    assert schedule.headers["location"].endswith("/login")
    assert hidden.status_code == 404
