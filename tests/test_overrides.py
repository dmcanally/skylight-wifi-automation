from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest
from prometheus_client import CollectorRegistry
from starlette.testclient import TestClient
from tests.conftest import PEM, chicago
from tests.fakes import Skylight, UniFiAPI, network_object
from tests.uiutil import ORIGIN, build, csrf_from, db_rows, poll, sign_in

from skylight_wifi_automation.evaluator import decide
from skylight_wifi_automation.metrics import Metrics
from skylight_wifi_automation.models import Task
from skylight_wifi_automation.overrides import override_expiry
from skylight_wifi_automation.service import AutomationService
from skylight_wifi_automation.unifi import UnifiClient

CHICAGO = ZoneInfo("America/Chicago")


def _task(complete: bool) -> Task:
    return Task("1", "Child A", "Dishes", complete, False, False, False)


def _active(bundle) -> list[sqlite3.Row]:
    return db_rows(bundle.store.path, "SELECT * FROM overrides WHERE cleared_at IS NULL")


def _set(bundle, action: str, confirmation: str = "") -> None:
    async def run() -> None:
        child = (await bundle.store.list_children())[0]
        await bundle.store.set_override(child.id, action, "admin", bundle.clock(), confirmation)

    asyncio.run(run())


def test_unblock_before_cutoff_expires_at_todays_cutoff(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 18), enabled=True)
    bundle.sky.tasks = [_task(False)]
    alone = decide(
        bundle.service.config.children[0],
        bundle.sky.tasks,
        bundle.clock(),
        bundle.service.config,
        already_unlocked=False,
    )
    assert alone.should_unlock is False
    _set(bundle, "unblock")
    poll(bundle)
    assert bundle.api.objects[0]["enabled"] is False
    assert bundle.service.last_poll["child-a"].reason == "manual unblock"
    assert _active(bundle)[0]["expires_at"].startswith("2026-08-25T01:30:00")
    bundle.clock.moment = chicago(2026, 8, 24, 20, 30)
    poll(bundle)
    assert _active(bundle) == []
    assert bundle.api.objects[0]["enabled"] is True
    assert bundle.service.last_poll["child-a"].reason == "daily cutoff reached"


def test_block_before_cutoff_expires_at_todays_cutoff_then_evaluator_resumes(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 18), enabled=False)
    bundle.sky.tasks = [_task(True)]
    alone = decide(
        bundle.service.config.children[0],
        bundle.sky.tasks,
        bundle.clock(),
        bundle.service.config,
        already_unlocked=False,
    )
    assert alone.should_unlock is True
    _set(bundle, "block")
    poll(bundle)
    assert bundle.api.objects[0]["enabled"] is True
    assert bundle.service.last_poll["child-a"].reason == "manual block"
    assert _active(bundle)[0]["expires_at"].startswith("2026-08-25T01:30:00")
    bundle.clock.moment = chicago(2026, 8, 24, 20, 30)
    poll(bundle)
    assert _active(bundle) == []
    assert bundle.service.last_poll["child-a"].reason == "daily cutoff reached"


def test_unblock_after_cutoff_expires_at_next_local_midnight(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 21), enabled=True)
    bundle.sky.tasks = [_task(False)]
    _set(bundle, "unblock", "UNBLOCK")
    assert _active(bundle)[0]["expires_at"].startswith("2026-08-25T05:00:00")
    assert not _active(bundle)[0]["expires_at"].startswith("2026-08-26")
    poll(bundle)
    assert bundle.api.objects[0]["enabled"] is False
    bundle.clock.moment = chicago(2026, 8, 24, 23, 30)
    poll(bundle)
    assert bundle.api.objects[0]["enabled"] is False
    assert _active(bundle)
    bundle.clock.moment = chicago(2026, 8, 25, 0)
    poll(bundle)
    assert _active(bundle) == []
    assert bundle.api.objects[0]["enabled"] is True
    assert bundle.service.last_poll["child-a"].reason == "required tasks remain"


def test_block_after_cutoff_expires_at_next_local_midnight(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 21), enabled=False)
    bundle.sky.tasks = [_task(True)]
    _set(bundle, "block")
    assert _active(bundle)[0]["expires_at"].startswith("2026-08-25T05:00:00")
    poll(bundle)
    assert bundle.api.objects[0]["enabled"] is True
    assert bundle.service.last_poll["child-a"].reason == "manual block"
    bundle.clock.moment = chicago(2026, 8, 25, 0, 10)
    poll(bundle)
    assert _active(bundle) == []
    assert bundle.api.objects[0]["enabled"] is False
    assert bundle.service.last_poll["child-a"].reason == "all required tasks complete"


def test_unblock_then_block_leaves_one_override(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 18))
    _set(bundle, "unblock")
    first = _active(bundle)[0]["expires_at"]
    bundle.clock.moment = chicago(2026, 8, 24, 21)
    _set(bundle, "block")
    active = _active(bundle)
    assert len(active) == 1
    assert active[0]["action"] == "block"
    assert active[0]["expires_at"] != first
    assert active[0]["expires_at"].startswith("2026-08-25T05:00:00")
    replaced = db_rows(bundle.store.path, "SELECT clear_reason FROM overrides WHERE cleared_at IS NOT NULL")
    assert [row["clear_reason"] for row in replaced] == ["replaced"]


def test_block_then_unblock_leaves_one_override(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 18))
    _set(bundle, "block")
    bundle.clock.moment = chicago(2026, 8, 24, 21)
    _set(bundle, "unblock", "UNBLOCK")
    active = _active(bundle)
    assert len(active) == 1
    assert active[0]["action"] == "unblock"
    assert active[0]["expires_at"].startswith("2026-08-25T05:00:00")
    assert len(db_rows(bundle.store.path, "SELECT id FROM overrides")) == 2


def test_expiry_hands_back_to_the_evaluator(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 18), enabled=True)
    bundle.sky.tasks = [_task(False)]
    _set(bundle, "unblock")
    poll(bundle)
    assert bundle.service.last_poll["child-a"].reason == "manual unblock"
    bundle.clock.moment = chicago(2026, 8, 24, 20, 30)
    poll(bundle)
    assert bundle.service.last_poll["child-a"].reason == "daily cutoff reached"
    assert bundle.service.last_poll["child-a"].blocked is True


def test_midnight_reset_hands_back_to_the_evaluator(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 21), enabled=True)
    bundle.sky.tasks = [_task(False)]
    _set(bundle, "unblock", "UNBLOCK")
    poll(bundle)
    assert bundle.service.last_poll["child-a"].blocked is False
    bundle.clock.moment = chicago(2026, 8, 25, 0)
    poll(bundle)
    assert bundle.service.last_poll["child-a"].reason == "required tasks remain"
    assert bundle.service.last_poll["child-a"].blocked is True


def test_spring_forward_midnight_uses_central_daylight_time(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 3, 8, 21))
    _set(bundle, "unblock", "UNBLOCK")
    expires = _active(bundle)[0]["expires_at"]
    assert expires.startswith("2026-03-09T05:00:00")
    expected = override_expiry(bundle.clock(), time(20, 30), "America/Chicago")
    assert expected == chicago(2026, 3, 9, 0).astimezone(UTC)
    bundle.clock.moment = chicago(2026, 3, 8, 23, 30)
    poll(bundle)
    assert _active(bundle)
    bundle.clock.moment = chicago(2026, 3, 9, 0)
    poll(bundle)
    assert _active(bundle) == []


def test_fall_back_midnight_uses_central_standard_time(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 11, 1, 21))
    _set(bundle, "block")
    expires = _active(bundle)[0]["expires_at"]
    assert expires.startswith("2026-11-02T06:00:00")
    assert override_expiry(bundle.clock(), time(20, 30), "America/Chicago") == chicago(2026, 11, 2, 0).astimezone(UTC)


def test_fall_back_ambiguous_hour_uses_todays_cutoff(tmp_path):
    for fold in (0, 1):
        moment = datetime(2026, 11, 1, 1, 30, tzinfo=CHICAGO, fold=fold)
        folder = tmp_path / f"fold-{fold}"
        folder.mkdir()
        bundle = build(folder, moment=moment)
        _set(bundle, "unblock")
        expires = _active(bundle)[0]["expires_at"]
        assert expires.startswith("2026-11-02T02:30:00")


def test_cutoff_at_midnight_is_a_single_instant():
    evening = chicago(2026, 8, 24, 18)
    expiry = override_expiry(evening, time(0, 0), "America/Chicago")
    midnight = chicago(2026, 8, 25, 0).astimezone(UTC)
    assert expiry == midnight
    at_midnight = chicago(2026, 8, 25, 0)
    assert override_expiry(at_midnight, time(0, 0), "America/Chicago") == at_midnight.astimezone(UTC)


def test_past_cutoff_unblock_requires_typed_confirmation(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 21))
    with TestClient(bundle.app) as client:
        sign_in(client)
        page = client.get("/")
        assert "2026-08-25 00:00" in page.text
        assert 'name="confirm"' in page.text
        child_id = db_rows(bundle.store.path, "SELECT id FROM children")[0]["id"]
        missing = client.post(
            f"/overrides/{child_id}",
            data={"csrf_token": csrf_from(page.text), "action": "unblock"},
            headers=ORIGIN,
        )
        assert missing.status_code == 400
        assert "UNBLOCK" in missing.text
        assert "2026-08-25 00:00" in missing.text
        wrong = client.post(
            f"/overrides/{child_id}",
            data={"csrf_token": csrf_from(missing.text), "action": "unblock", "confirm": "unblock"},
            headers=ORIGIN,
        )
        assert wrong.status_code == 400
        assert _active(bundle) == []
        saved = client.post(
            f"/overrides/{child_id}",
            data={"csrf_token": csrf_from(wrong.text), "action": "unblock", "confirm": "UNBLOCK"},
            headers=ORIGIN,
            follow_redirects=False,
        )
    assert saved.status_code == 303
    assert _active(bundle)[0]["expires_at"].startswith("2026-08-25T05:00:00")


def test_before_cutoff_unblock_is_one_click(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 18))
    with TestClient(bundle.app) as client:
        sign_in(client)
        page = client.get("/")
        assert "2026-08-24 20:30" in page.text
        assert 'name="confirm"' not in page.text
        child_id = db_rows(bundle.store.path, "SELECT id FROM children")[0]["id"]
        saved = client.post(
            f"/overrides/{child_id}",
            data={"csrf_token": csrf_from(page.text), "action": "unblock"},
            headers=ORIGIN,
            follow_redirects=False,
        )
    assert saved.status_code == 303
    assert _active(bundle)[0]["action"] == "unblock"


def test_override_with_dry_run_makes_no_writes(tmp_path, caplog):
    caplog.set_level("INFO")
    bundle = build(tmp_path, dry_run=True, moment=chicago(2026, 8, 24, 18), enabled=True)
    bundle.sky.tasks = [_task(False)]
    _set(bundle, "unblock")
    poll(bundle)
    assert bundle.api.writes() == []
    assert "action=pause" in caplog.text
    assert "write=False" in caplog.text
    assert "unlock=True" in caplog.text


def test_restart_keeps_the_active_override(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 18), enabled=True)
    bundle.sky.tasks = [_task(False)]
    _set(bundle, "unblock")
    api = UniFiAPI([network_object(enabled=True)])
    unifi = UnifiClient(
        "test-api-key",
        "https://unifi.local/proxy/network/integration/v1",
        PEM,
        requester=api,
    )
    config = asyncio.run(bundle.store.read_config())
    restarted = AutomationService(
        config,
        Skylight(bundle.sky.tasks),
        unifi,
        Metrics(CollectorRegistry()),
        clock=bundle.clock,
        latch_path=tmp_path / "restart-latch.json",
        overrides=bundle.store,
    )
    asyncio.run(restarted.poll())
    assert api.objects[0]["enabled"] is False
    assert restarted.last_poll["child-a"].reason == "manual unblock"


def test_clear_returns_to_the_evaluator(tmp_path):
    bundle = build(tmp_path, moment=chicago(2026, 8, 24, 18), enabled=False)
    bundle.sky.tasks = [_task(False)]
    _set(bundle, "unblock")

    async def clear() -> None:
        child = (await bundle.store.list_children())[0]
        assert await bundle.store.clear_override(child.id, "admin", bundle.clock())

    asyncio.run(clear())
    poll(bundle)
    assert _active(bundle) == []
    assert bundle.service.last_poll["child-a"].reason == "required tasks remain"
    assert bundle.api.writes()[0]["body"]["enabled"] is True


def test_only_one_active_override_is_allowed_by_the_schema(tmp_path):
    bundle = build(tmp_path)
    _set(bundle, "block")
    child_id = _active(bundle)[0]["child_id"]
    connection = sqlite3.connect(bundle.store.path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO overrides(child_id, action, expires_at, set_by, created_at)
               VALUES (?, 'unblock', '2026-08-25T01:30:00+00:00', 'admin', '2026-08-24T23:00:00+00:00')""",
            (child_id,),
        )
        connection.commit()
    connection.close()


def test_list_sites_is_a_get_of_the_existing_collection():
    api = UniFiAPI()
    client = UnifiClient(
        "test-api-key",
        "https://unifi.local/proxy/network/integration/v1",
        PEM,
        requester=api,
    )

    async def run() -> None:
        sites = await client.list_sites()
        assert len(sites) == 1

    asyncio.run(run())
    assert [call["method"] for call in api.calls] == ["GET"]
    assert api.calls[0]["url"].endswith("/sites")
