from __future__ import annotations

import asyncio
import logging

import pytest
from cryptography.exceptions import InvalidTag
from starlette.testclient import TestClient
from tests.uiutil import ORIGIN, build, csrf_from, db_rows, login, sign_in

from skylight_wifi_automation.clients import SkylightClient
from skylight_wifi_automation.redact import REDACTOR
from skylight_wifi_automation.secrets import decrypt, encrypt
from skylight_wifi_automation.store import Store
from skylight_wifi_automation.unifi import UnifiClient

SECRET = "skylight-secret-value"
EMAIL = "person@example.com"
API_KEY = "unifi-secret-value"


def _pages(client: TestClient) -> str:
    chunks = []
    for path in ("/", "/settings", "/children", "/unifi", "/secrets", "/audit", "/password"):
        response = client.get(path)
        assert response.status_code == 200
        chunks.append(response.text)
    return "\n".join(chunks)


def test_secret_round_trip_and_wrong_key():
    key = b"a" * 32
    other = b"b" * 32
    nonce, ciphertext = encrypt(key, SECRET, "UNIFI_API_KEY")
    value, legacy = decrypt(key, nonce, ciphertext, "UNIFI_API_KEY")
    assert value == SECRET
    assert legacy is False
    with pytest.raises(InvalidTag):
        decrypt(other, nonce, ciphertext, "UNIFI_API_KEY")


def test_values_stay_out_of_pages_logs_audit_and_errors(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("SKYLIGHT_EMAIL", EMAIL)
    monkeypatch.setenv("SKYLIGHT_PASSWORD", SECRET)
    monkeypatch.setenv("UNIFI_API_KEY", API_KEY)
    caplog.set_level(logging.INFO)
    bundle = build(tmp_path)

    async def save() -> None:
        now = bundle.clock()
        await bundle.store.set_secret("UNIFI_API_KEY", API_KEY, "admin", now)
        await bundle.store.set_secret("SKYLIGHT_PASSWORD", SECRET, "admin", now)

    asyncio.run(save())
    with TestClient(bundle.app) as client:
        sign_in(client)

        async def explode(secrets, config):
            raise RuntimeError(SECRET)

        bundle.app.state.context.tester = explode
        token = csrf_from(client.get("/secrets").text)
        tested = client.post(
            "/secrets/test",
            data={"csrf_token": token},
            headers=ORIGIN,
            follow_redirects=True,
        )
        rendered = _pages(client) + tested.text
    audit = "\n".join(row["diff"] for row in db_rows(bundle.store.path, "SELECT diff FROM audit"))
    logging.getLogger("skylight_wifi_automation.demo").info("leak %s", SECRET)
    try:
        raise RuntimeError(API_KEY)
    except RuntimeError:
        logging.getLogger("skylight_wifi_automation.demo").exception("boom")
    for secret in (SECRET, API_KEY, EMAIL):
        assert secret not in rendered
        assert secret not in audit
        assert secret not in caplog.text
    assert "source db" in rendered
    assert "[redacted]" in caplog.text
    assert "skylight_error" in tested.text
    assert SECRET not in repr(bundle.runtime)
    assert API_KEY not in repr(bundle.service.unifi)


def test_env_fallback_and_database_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "env-api-key-value")
    bundle = build(tmp_path)

    async def read_source() -> str:
        resolved = await bundle.store.resolve_secrets()
        return resolved.api_key, resolved.sources["UNIFI_API_KEY"]

    value, source = asyncio.run(read_source())
    assert value == "env-api-key-value" and source == "env"
    with TestClient(bundle.app) as client:
        sign_in(client)
        page = client.get("/secrets")
        assert "source env" in page.text
        assert "env-api-key-value" not in page.text
        token = csrf_from(page.text)
        saved = client.post(
            "/secrets",
            data={"csrf_token": token, "UNIFI_API_KEY": "db-api-key-value"},
            headers=ORIGIN,
            follow_redirects=True,
        )
    assert "db-api-key-value" not in saved.text
    assert "env-api-key-value" not in saved.text
    assert "source db" in saved.text

    async def read_db() -> str:
        resolved = await bundle.store.resolve_secrets()
        return resolved.api_key

    assert asyncio.run(read_db()) == "db-api-key-value"
    updated = db_rows(bundle.store.path, "SELECT COUNT(*) AS n FROM audit WHERE action = 'secret_updated'")[0]["n"]
    with TestClient(bundle.app) as client:
        assert login(client).status_code == 303
        token = csrf_from(client.get("/secrets").text)
        client.post(
            "/secrets",
            data={"csrf_token": token, "UNIFI_API_KEY": ""},
            headers=ORIGIN,
            follow_redirects=False,
        )
        token = csrf_from(client.get("/secrets").text)
        cleared = client.post(
            "/secrets/UNIFI_API_KEY/clear",
            data={"csrf_token": token},
            headers=ORIGIN,
            follow_redirects=True,
        )
    assert db_rows(bundle.store.path, "SELECT COUNT(*) AS n FROM audit WHERE action = 'secret_updated'")[0]["n"] == updated
    assert "source env" in cleared.text
    assert "db-api-key-value" not in cleared.text
    audit = " ".join(row["diff"] for row in db_rows(bundle.store.path, "SELECT diff FROM audit"))
    assert "db-api-key-value" not in audit
    assert "cleared" in audit


def test_missing_encryption_key_disables_the_secrets_form(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "env-api-key-value")
    bundle = build(tmp_path, key=None)
    with TestClient(bundle.app) as client:
        sign_in(client)
        page = client.get("/secrets")
        posted = client.post("/secrets", data={"UNIFI_API_KEY": "db-api-key-value"}, headers=ORIGIN)
    assert "APP_ENCRYPTION_KEY" in page.text
    assert "env-api-key-value" not in page.text
    assert posted.status_code == 403
    assert "db-api-key-value" not in posted.text


def test_legacy_secret_is_rewritten_with_associated_data(tmp_path, monkeypatch):
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    monkeypatch.delenv("UNIFI_API_KEY", raising=False)
    monkeypatch.delenv("SKYLIGHT_EMAIL", raising=False)
    monkeypatch.delenv("SKYLIGHT_PASSWORD", raising=False)
    key = b"c" * 32
    store = Store(tmp_path / "app.db", key)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, b"legacy-secret-value", None)

    def insert() -> None:
        with store._lock:
            store._conn.execute(
                "INSERT INTO secrets(name, nonce, ciphertext, updated_at) VALUES (?, ?, ?, ?)",
                ("UNIFI_API_KEY", nonce, ciphertext, "2026-08-24T00:00:00+00:00"),
            )

    insert()

    async def read() -> str:
        resolved = await store.resolve_secrets()
        return resolved.api_key

    assert asyncio.run(read()) == "legacy-secret-value"
    row = db_rows(store.path, "SELECT nonce, ciphertext FROM secrets")[0]
    value, legacy = decrypt(key, row["nonce"], row["ciphertext"], "UNIFI_API_KEY")
    assert legacy is False
    assert value == "legacy-secret-value"
    with pytest.raises(InvalidTag):
        AESGCM(key).decrypt(row["nonce"], row["ciphertext"], None)


def test_rotate_key_rewrites_secrets_without_printing_them(tmp_path, monkeypatch, caplog):
    import base64
    from datetime import UTC, datetime

    from skylight_wifi_automation.main import rotate_key

    monkeypatch.delenv("UNIFI_API_KEY", raising=False)
    key = b"e" * 32
    new_key = b"f" * 32
    store = Store(tmp_path / "app.db", key)

    async def save() -> None:
        await store.set_secret("UNIFI_API_KEY", "rotated-secret-value", "cli", datetime.now(UTC))

    asyncio.run(save())
    store.close()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENCRYPTION_KEY", base64.b64encode(key).decode())
    monkeypatch.setenv("APP_ENCRYPTION_KEY_NEW", base64.b64encode(new_key).decode())
    asyncio.run(rotate_key())
    assert "rotated-secret-value" not in caplog.text
    opened = Store(tmp_path / "app.db", new_key)

    async def read() -> str:
        return (await opened.resolve_secrets()).api_key

    assert asyncio.run(read()) == "rotated-secret-value"
    opened.close()


def test_client_repr_hides_credentials():
    sky = SkylightClient("person@example.com", SECRET)
    unifi = UnifiClient(API_KEY, "https://unifi.local/proxy/network/integration/v1", "pem", None)
    assert SECRET not in repr(sky)
    assert "person@example.com" not in repr(sky)
    assert API_KEY not in repr(unifi)
    REDACTOR.update([SECRET])
    assert REDACTOR.scrub_text(f"password={SECRET}") == "password=[redacted]"
