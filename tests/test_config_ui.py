from __future__ import annotations

import asyncio
import logging
from datetime import time

import pytest
from starlette.testclient import TestClient
from tests.conftest import chicago
from tests.uiutil import ORIGIN, build, csrf_from, db_rows, poll, sign_in

from skylight_wifi_automation.models import Task


def _done() -> Task:
    return Task("1", "Child A", "Dishes", True, False, False, False)


def test_validation_error_and_invalid_save_keep_the_snapshot(tmp_path):
    bundle = build(tmp_path)
    version = asyncio.run(bundle.store.config_version())
    with TestClient(bundle.app) as client:
        token = sign_in(client)
        response = client.post(
            "/settings",
            data={
                "csrf_token": token,
                "timezone": "America/Chicago",
                "poll_seconds": "1",
                "dry_run": "false",
                "include_late": "true",
                "include_routines": "true",
                "empty_day_unlocks": "false",
            },
            headers=ORIGIN,
        )
        token = csrf_from(client.get("/unifi").text)
        certificate = client.post(
            "/unifi",
            data={
                "csrf_token": token,
                "unifi_url": "https://unifi.local/proxy/network/integration/v1",
                "unifi_site": "",
                "unifi_tls_ca": "-----BEGIN CERTIFICATE-----\n<paste your console certificate>\n-----END CERTIFICATE-----",
            },
            headers=ORIGIN,
        )
    assert response.status_code == 400
    assert "poll_seconds" in response.text
    assert certificate.status_code == 400
    assert "unifi_tls_ca" in certificate.text
    assert "<paste your console certificate>" not in bundle.service.config.unifi_tls_ca
    assert asyncio.run(bundle.store.config_version()) == version
    assert bundle.service.config.poll_seconds == 60
    connection = __import__("sqlite3").connect(bundle.store.path)
    connection.execute("UPDATE settings SET value = '1' WHERE key = 'poll_seconds'")
    connection.execute("UPDATE config_version SET version = version + 1")
    connection.commit()
    connection.close()
    poll(bundle)
    assert bundle.service.config.poll_seconds == 60


def test_hot_reload_picks_up_a_cutoff_without_restart(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 19))
    bundle.sky.tasks = [_done()]
    poll(bundle)
    assert bundle.api.objects[0]["enabled"] is False
    with TestClient(bundle.app) as client:
        token = sign_in(client)
        child_id = db_rows(bundle.store.path, "SELECT id FROM children")[0]["id"]
        saved = client.post(
            f"/children/{child_id}",
            data={
                "csrf_token": token,
                "label": "child-a",
                "skylight_profile": "Child A",
                "object_name": "No Internet - child-a",
                "cutoff": "18:00",
            },
            headers=ORIGIN,
            follow_redirects=False,
        )
    assert saved.status_code == 303
    poll(bundle)
    assert bundle.service.config.children[0].cutoff == time(18, 0)
    assert bundle.api.objects[0]["enabled"] is True


def test_yaml_is_imported_once_and_drift_warns_once(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    bundle = build(tmp_path)
    assert [row["label"] for row in db_rows(bundle.store.path, "SELECT label FROM children")] == ["child-a"]
    assert any(row["action"] == "import" for row in db_rows(bundle.store.path, "SELECT action FROM audit"))
    text = bundle.yaml_path.read_text(encoding="utf-8").replace("America/Chicago", "America/New_York")
    bundle.yaml_path.write_text(text, encoding="utf-8")

    async def again() -> None:
        imported = await bundle.store.import_config(
            __import__("skylight_wifi_automation.config", fromlist=["load_config"]).load_config(bundle.yaml_path),
            bundle.clock(),
            "different",
        )
        assert imported is False
        await bundle.runtime.warn_if_drift()
        await bundle.runtime.warn_if_drift()

    asyncio.run(again())
    assert [row["label"] for row in db_rows(bundle.store.path, "SELECT label FROM children")] == ["child-a"]
    warnings = [rec.message for rec in caplog.records if "source of truth" in rec.message]
    assert len(warnings) == 1


def test_manual_unblock_migrates_to_an_override_and_old_yaml_still_loads(tmp_path):
    bundle = build(tmp_path, manual=True, moment=chicago(2026, 8, 24, 18))
    config = asyncio.run(bundle.store.read_config())
    assert config.children[0].manual_unblock is False
    rows = db_rows(bundle.store.path, "SELECT action, expires_at, set_by FROM overrides WHERE cleared_at IS NULL")
    assert len(rows) == 1
    assert rows[0]["action"] == "unblock"
    assert rows[0]["set_by"] == "import"
    assert rows[0]["expires_at"].startswith("2026-08-25T01:30:00")
    loaded = __import__("skylight_wifi_automation.config", fromlist=["load_config"]).load_config(bundle.yaml_path)
    assert loaded.children[0].manual_unblock is True


def test_file_mode_is_read_only_and_keeps_manual_unblock(tmp_path):
    bundle = build(tmp_path, source="file", manual=True, moment=chicago(2026, 8, 24, 18))
    original = bundle.yaml_path.read_text(encoding="utf-8")
    bundle.sky.tasks = [Task("1", "Child A", "Dishes", False, False, False, False)]
    with TestClient(bundle.app) as client:
        token = sign_in(client)
        denied = client.post(
            "/settings",
            data={"csrf_token": token, "timezone": "UTC", "poll_seconds": "60"},
            headers=ORIGIN,
        )
        page = client.get("/")
    assert denied.status_code == 403
    assert bundle.yaml_path.read_text(encoding="utf-8") == original
    assert "read-only" in page.text
    poll(bundle)
    assert bundle.api.writes()[0]["body"]["enabled"] is False
    assert bundle.service.config.children[0].manual_unblock is True


def test_database_uses_wal(tmp_path):
    bundle = build(tmp_path)
    assert asyncio.run(bundle.store.journal_mode()) == "wal"


def test_database_files_are_private(tmp_path):
    from pathlib import Path

    store_path = tmp_path / "app.db"
    bundle = build(tmp_path)
    assert store_path.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    for suffix in ("-wal", "-shm"):
        extra = Path(f"{store_path}{suffix}")
        if extra.exists():
            assert extra.stat().st_mode & 0o777 == 0o600
    del bundle


def test_missing_or_unusable_data_dir_is_one_line(tmp_path, monkeypatch):
    from skylight_wifi_automation.runtime import database_path
    from skylight_wifi_automation.store import Store

    missing = tmp_path / "missing"
    monkeypatch.setenv("DATA_DIR", str(missing))
    with pytest.raises(RuntimeError, match=r"^DATA_DIR is not writable: .+$") as missing_error:
        database_path()
    assert "\n" not in str(missing_error.value)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"^DATA_DIR is not writable:"):
        Store(blocked / "app.db", None)


def test_unknown_timezone_is_a_form_error(tmp_path):
    bundle = build(tmp_path)
    with TestClient(bundle.app) as client:
        token = sign_in(client)
        response = client.post(
            "/settings",
            data={
                "csrf_token": token,
                "timezone": "Not/AZone",
                "poll_seconds": "60",
                "dry_run": "false",
                "include_late": "true",
                "include_routines": "true",
                "empty_day_unlocks": "false",
            },
            headers=ORIGIN,
        )
    assert response.status_code == 400
    assert "timezone" in response.text
    assert bundle.service.config.timezone == "America/Chicago"
