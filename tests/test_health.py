from __future__ import annotations

from starlette.testclient import TestClient
from tests.uiutil import build

from skylight_wifi_automation import __version__


def test_healthz_is_public_and_leaks_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "unifi-secret-value")
    monkeypatch.setenv("SKYLIGHT_EMAIL", "person@example.com")
    monkeypatch.setenv("SKYLIGHT_PASSWORD", "skylight-secret-value")
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
    body = response.text
    for secret in (
        "unifi-secret-value",
        "skylight-secret-value",
        "person@example.com",
        "child-a",
        "dry_run",
        "unifi_tls_ca",
        "No Internet",
    ):
        assert secret not in body
    for header in ("content-security-policy", "x-content-type-options", "referrer-policy"):
        assert header in {name.lower() for name in response.headers}
    assert response.headers["content-security-policy"] == "default-src 'self'; frame-ancestors 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"


def test_static_css_is_public_and_has_security_headers(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        response = client.get("/static/app.css")
        denied = client.post("/static/app.css", follow_redirects=False)
    assert response.status_code == 200
    assert "Skylight" not in response.text or "button" in response.text
    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert denied.status_code == 401
