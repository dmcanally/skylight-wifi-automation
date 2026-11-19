from datetime import timedelta
from zoneinfo import ZoneInfo

from prometheus_client import CollectorRegistry, generate_latest
from tests.conftest import (
    API_KEY,
    CLIENT_IP,
    MAC,
    PASSWORD,
    app_config,
    chicago,
    child,
)
from tests.fakes import Skylight, UniFiAPI, network_object

from skylight_wifi_automation.metrics import Metrics
from skylight_wifi_automation.models import Task
from skylight_wifi_automation.service import AutomationService
from skylight_wifi_automation.unifi import UnifiClient


def _task(**overrides):
    values = {
        "id": "1",
        "profile": "Lanie",
        "summary": "Dishes",
        "complete": False,
        "skipped": False,
        "routine": False,
        "late": False,
    }
    values.update(overrides)
    return Task(**values)


def _build(tmp_path, tasks, objects, when, **config):
    registry_metrics = Metrics(CollectorRegistry())
    api = UniFiAPI(objects)
    unifi = UnifiClient(
        API_KEY,
        "https://unifi.local/proxy/network/integration/v1",
        app_config().unifi_tls_ca,
        requester=api,
    )
    service = AutomationService(
        app_config(**config),
        Skylight(tasks),
        unifi,
        registry_metrics,
        clock=lambda: when,
        latch_path=tmp_path / "latch.json",
    )
    return service, api, registry_metrics


async def test_dry_run_logs_the_toggle_and_never_writes(tmp_path, caplog):
    caplog.set_level("INFO")
    service, api, metrics = _build(
        tmp_path,
        [_task(complete=True)],
        [network_object(enabled=True)],
        chicago(2026, 8, 24, 18),
        dry_run=True,
    )
    await service.poll()
    assert api.writes() == []
    assert "unlock=True" in caplog.text
    assert "lock=False" in caplog.text
    assert "action=pause" in caplog.text
    assert "write=False" in caplog.text
    text = generate_latest(metrics._registry).decode()
    assert "wifi_automation_poll_failures_total 0.0" in text
    assert "wifi_automation_last_success_timestamp_seconds" in text
    assert API_KEY not in caplog.text
    assert PASSWORD not in caplog.text
    assert MAC not in caplog.text
    assert CLIENT_IP not in caplog.text


async def test_idempotent_read_does_not_write_when_state_matches(tmp_path):
    service, api, _metrics = _build(
        tmp_path,
        [_task(complete=True)],
        [network_object(enabled=False)],
        chicago(2026, 8, 24, 18),
    )
    await service.poll()
    await service.poll()
    assert api.writes() == []


async def test_pause_and_resume_put_only_the_enabled_flag(tmp_path):
    service, api, _metrics = _build(
        tmp_path,
        [_task(complete=True)],
        [network_object(enabled=True)],
        chicago(2026, 8, 24, 18),
    )
    await service.poll()
    writes = api.writes()
    assert len(writes) == 1
    assert writes[0]["method"] == "PUT"
    assert writes[0]["url"].endswith("/object-oriented-network-config/obj-lanie")
    assert "firewall" not in writes[0]["url"]
    assert writes[0]["body"]["enabled"] is False
    assert writes[0]["body"]["targets"] == [{"type": "MAC", "value": MAC}]
    assert writes[0]["body"]["client_address"] == CLIENT_IP
    assert all(call["method"] in {"GET", "PUT"} for call in api.calls)


async def test_daily_latch_does_not_reblock_until_cutoff_or_the_next_day(tmp_path):
    earned, api, _metrics = _build(
        tmp_path,
        [_task(complete=True)],
        [network_object(enabled=True)],
        chicago(2026, 8, 24, 18),
    )
    await earned.poll()
    assert api.objects[0]["enabled"] is False

    blocked_again = network_object(enabled=True)
    api.objects[:] = [blocked_again]
    later, later_api, _metrics = _build(
        tmp_path,
        [_task(complete=False)],
        [blocked_again],
        chicago(2026, 8, 24, 19),
    )
    later.unlocked_on = dict(earned.unlocked_on)
    await later.poll()
    assert later_api.writes()[0]["body"]["enabled"] is False

    cutoff, cutoff_api, _metrics = _build(
        tmp_path,
        [_task(complete=True)],
        [network_object(enabled=False)],
        chicago(2026, 8, 24, 20, 30),
    )
    cutoff.unlocked_on = dict(earned.unlocked_on)
    await cutoff.poll()
    assert cutoff_api.writes()[0]["body"]["enabled"] is True

    nxt, nxt_api, _metrics = _build(
        tmp_path,
        [_task(complete=False)],
        [network_object(enabled=False)],
        chicago(2026, 8, 25, 18),
    )
    nxt.unlocked_on = dict(earned.unlocked_on)
    await nxt.poll()
    assert nxt_api.writes()[0]["body"]["enabled"] is True


async def test_latch_file_survives_a_new_process(tmp_path):
    first, _api, _metrics = _build(
        tmp_path,
        [_task(complete=True)],
        [network_object(enabled=True)],
        chicago(2026, 8, 24, 18),
    )
    await first.poll()
    second, api, _metrics = _build(
        tmp_path,
        [_task(complete=False)],
        [network_object(enabled=True)],
        chicago(2026, 8, 24, 19),
    )
    await second.poll()
    assert api.writes()[0]["body"]["enabled"] is False


async def test_empty_day_stays_blocked_and_manual_unblock_stops_at_cutoff(tmp_path):
    empty, empty_api, _metrics = _build(
        tmp_path,
        [],
        [network_object(enabled=False)],
        chicago(2026, 8, 24, 18),
    )
    await empty.poll()
    assert empty_api.writes()[0]["body"]["enabled"] is True

    opened, opened_api, _metrics = _build(
        tmp_path,
        [],
        [network_object(enabled=True)],
        chicago(2026, 8, 24, 18),
        empty_day_unlocks=True,
    )
    await opened.poll()
    assert opened_api.writes()[0]["body"]["enabled"] is False

    manual, manual_api, _metrics = _build(
        tmp_path,
        [_task()],
        [network_object(enabled=True)],
        chicago(2026, 8, 24, 18),
        children=[child(manual_unblock=True)],
    )
    await manual.poll()
    assert manual_api.writes()[0]["body"]["enabled"] is False

    locked, locked_api, _metrics = _build(
        tmp_path,
        [_task(complete=True)],
        [network_object(enabled=False)],
        chicago(2026, 8, 24, 20, 30),
        children=[child(manual_unblock=True)],
    )
    await locked.poll()
    assert locked_api.writes()[0]["body"]["enabled"] is True


async def test_cutoff_uses_chicago_time_when_the_clock_returns_utc(tmp_path):
    winter = chicago(2026, 1, 15, 20, 30)
    summer = chicago(2026, 7, 15, 20, 30)
    assert winter.utcoffset() != summer.utcoffset()
    for local in (winter, summer):
        service, api, _metrics = _build(
            tmp_path,
            [_task(complete=True)],
            [network_object(enabled=False)],
            local.astimezone(ZoneInfo("UTC")),
        )
        await service.poll()
        assert api.writes()[0]["body"]["enabled"] is True
        assert local.utcoffset() in {timedelta(hours=-6), timedelta(hours=-5)}


async def test_skylight_failure_leaves_state_until_cutoff(tmp_path, caplog):
    caplog.set_level("INFO")
    morning, morning_api, morning_metrics = _build(
        tmp_path,
        [],
        [network_object(enabled=False)],
        chicago(2026, 8, 24, 18),
    )
    morning.skylight.error = RuntimeError(PASSWORD)
    await morning.poll()
    assert morning_api.writes() == []
    assert "unlock=False" in caplog.text and "lock=False" in caplog.text
    assert PASSWORD not in caplog.text
    assert "wifi_automation_last_success_timestamp_seconds" not in generate_latest(
        morning_metrics._registry
    ).decode()

    evening, evening_api, evening_metrics = _build(
        tmp_path,
        [],
        [network_object(enabled=False)],
        chicago(2026, 8, 24, 20, 30),
    )
    evening.skylight.error = RuntimeError(PASSWORD)
    await evening.poll()
    assert evening_api.writes()[0]["body"]["enabled"] is True
    assert "wifi_automation_poll_failures_total 1.0" in generate_latest(
        evening_metrics._registry
    ).decode()
    assert "wifi_automation_last_success_timestamp_seconds" not in generate_latest(
        evening_metrics._registry
    ).decode()


async def test_unifi_read_failure_writes_nothing_even_at_cutoff(tmp_path, caplog):
    caplog.set_level("INFO")
    api = UniFiAPI([network_object(enabled=False)], fail="objects")
    unifi = UnifiClient(API_KEY, "https://unifi.local/proxy/network/integration/v1", app_config().unifi_tls_ca, requester=api)
    metrics = Metrics(CollectorRegistry())
    service = AutomationService(
        app_config(),
        Skylight([_task(complete=True)], error=None),
        unifi,
        metrics,
        clock=lambda: chicago(2026, 8, 24, 20, 30),
        latch_path=tmp_path / "latch.json",
    )
    await service.poll()
    assert api.writes() == []
    assert API_KEY not in caplog.text
    assert "wifi_automation_poll_failures_total 1.0" in generate_latest(metrics._registry).decode()


async def test_startup_does_not_write_until_every_object_has_been_read(tmp_path):
    api = UniFiAPI(
        [network_object(enabled=True)],
    )
    unifi = UnifiClient(
        API_KEY,
        "https://unifi.local/proxy/network/integration/v1",
        app_config().unifi_tls_ca,
        requester=api,
    )
    service = AutomationService(
        app_config(children=[child("Lanie"), child("Libby")]),
        Skylight([_task(complete=True), _task(id="2", profile="Libby", complete=True)]),
        unifi,
        Metrics(CollectorRegistry()),
        clock=lambda: chicago(2026, 8, 24, 18),
        latch_path=tmp_path / "latch.json",
    )
    await service.poll()
    assert api.writes() == []

    api.fail = "sites"
    await service.poll()
    assert api.writes() == []


async def test_write_failure_stops_later_children(tmp_path):
    api = UniFiAPI(
        [
            network_object(enabled=True),
            network_object(name="No Internet - Libby", object_id="obj-libby", enabled=True),
        ],
        fail="write",
    )
    unifi = UnifiClient(
        API_KEY,
        "https://unifi.local/proxy/network/integration/v1",
        app_config().unifi_tls_ca,
        requester=api,
    )
    service = AutomationService(
        app_config(children=[child("Lanie"), child("Libby")]),
        Skylight([_task(complete=True), _task(id="2", profile="Libby", complete=True)]),
        unifi,
        Metrics(CollectorRegistry()),
        clock=lambda: chicago(2026, 8, 24, 18),
        latch_path=tmp_path / "latch.json",
    )
    await service.poll()
    assert len(api.writes()) == 1
    assert api.writes()[0]["url"].endswith("/obj-lanie")


async def test_object_lookup_follows_pages(tmp_path):
    api = UniFiAPI(
        [
            network_object(name="No Internet - Other", object_id="obj-other", enabled=True),
            network_object(enabled=True),
        ]
    )
    unifi = UnifiClient(
        API_KEY,
        "https://unifi.local/proxy/network/integration/v1",
        app_config().unifi_tls_ca,
        requester=api,
        page_limit=1,
    )
    service = AutomationService(
        app_config(),
        Skylight([_task(complete=True)]),
        unifi,
        Metrics(CollectorRegistry()),
        clock=lambda: chicago(2026, 8, 24, 18),
        latch_path=tmp_path / "latch.json",
    )
    await service.poll()
    offsets = [
        call["params"]["offset"]
        for call in api.calls
        if call["url"].endswith("object-oriented-network-configs")
    ]
    assert offsets == [0, 1]
    assert api.writes()[0]["body"]["enabled"] is False
    assert api.objects[0]["enabled"] is True
