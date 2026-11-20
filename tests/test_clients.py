from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from tests.conftest import API_KEY, PASSWORD, app_config
from tests.fakes import UniFiAPI, network_object

from skylight_wifi_automation.clients import SkylightClient
from skylight_wifi_automation.metrics import Metrics
from skylight_wifi_automation.service import AutomationService
from skylight_wifi_automation.unifi import UnifiClient

DAY = date(2026, 11, 19)
# 00:30 UTC on the 20th is 18:30 CST on the 19th, before the 20:30 cutoff.
WHEN = datetime(2026, 11, 20, 0, 30, tzinfo=UTC)


def _chore(
    chore_id: str,
    *,
    complete: bool,
    start: date | str = DAY,
    summary: str = "Dishes",
    routine: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=chore_id,
        summary=summary,
        start=start,
        status="complete" if complete else "pending",
        completed=complete,
        completed_on=start if complete else None,
        category_id="cat-child-a",
        routine=routine,
        raw={},
    )


class FakeSkylight:
    """Stand-in for the pyskylight client. No network."""

    def __init__(
        self,
        dated: list[Any] | None = None,
        groups: Any | None = None,
        error: BaseException | None = None,
        error_on: str = "get_chores",
    ) -> None:
        self.dated = list(dated or [])
        self.groups = groups if groups is not None else SimpleNamespace(chores={}, routines={})
        self.error = error
        self.error_on = error_on
        self.queries: list[dict[str, Any]] = []

    async def get_frames(self) -> list[Any]:
        self._maybe_fail("get_frames")
        return [SimpleNamespace(id="frame-1")]

    async def get_categories(self, frame_id: str) -> list[Any]:
        self._maybe_fail("get_categories")
        assert frame_id == "frame-1"
        return [SimpleNamespace(id="cat-child-a", label="Child A")]

    async def get_all_chores(self, frame_id: str, **params: Any) -> Any:
        self._maybe_fail("get_all_chores")
        assert frame_id == "frame-1"
        assert params == {}
        return self.groups

    async def get_chores(self, frame_id: str, **params: Any) -> list[Any]:
        self.queries.append(dict(params))
        self._maybe_fail("get_chores")
        assert frame_id == "frame-1"
        if params.get("after") == DAY and params.get("before") == DAY:
            return list(self.dated)
        return []

    def _maybe_fail(self, name: str) -> None:
        if self.error is not None and self.error_on == name:
            raise self.error


def _service(
    tmp_path, api: FakeSkylight, *, enabled: bool = True, latch: str = "latch.json", **config
):
    client = SkylightClient("person@example.com", PASSWORD)
    client._client = api
    unifi_api = UniFiAPI([network_object(enabled=enabled)])
    unifi = UnifiClient(
        API_KEY,
        "https://unifi.local/proxy/network/integration/v1",
        app_config().unifi_tls_ca,
        requester=unifi_api,
    )
    metrics = Metrics(CollectorRegistry())
    service = AutomationService(
        app_config(**config),
        client,
        unifi,
        metrics,
        clock=lambda: WHEN,
        latch_path=tmp_path / latch,
    )
    return service, unifi_api, metrics


def _assert_chicago_day(api: FakeSkylight) -> None:
    local = WHEN.astimezone(ZoneInfo("America/Chicago"))
    assert local.date() == DAY
    assert WHEN.date() != DAY
    assert api.queries
    assert all(query == {"after": DAY, "before": DAY} for query in api.queries)


async def test_both_chores_completed_is_two_of_two_and_unlocks(tmp_path, caplog):
    caplog.set_level("INFO")
    via_status = _chore("1", complete=True)
    via_status.completed = False
    via_status.completed_on = None
    via_status.status = "complete"
    via_completed_on = _chore("2", complete=True, summary="Laundry")
    via_completed_on.completed = False
    via_completed_on.status = "pending"
    tomorrow = _chore("3", complete=True, summary="Tomorrow", start=date(2026, 11, 20))
    # Completed chores are absent from get_all_chores. The date-range call has them.
    api = FakeSkylight(dated=[via_status, via_completed_on, tomorrow])
    service, unifi, _metrics = _service(tmp_path, api, enabled=True)
    tasks = await service.skylight.tasks_for(DAY)
    assert [(task.id, task.complete) for task in tasks] == [("1", True), ("2", True)]

    await service.poll()
    _assert_chicago_day(api)
    assert unifi.writes()[0]["body"]["enabled"] is False
    assert "tasks=2/2" in caplog.text
    assert "unlock=True" in caplog.text
    assert "lock=False" in caplog.text
    assert "all required tasks complete" in caplog.text


async def test_one_of_two_completed_stays_blocked(tmp_path, caplog):
    caplog.set_level("INFO")
    open_chore = _chore("1", complete=False, start="2026-11-19")
    done = _chore("2", complete=True, summary="Laundry")
    groups = SimpleNamespace(
        chores={
            "today": [open_chore],
            "future": [_chore("9", complete=False, start=date(2026, 11, 21))],
        },
        routines={},
    )
    api = FakeSkylight(
        dated=[open_chore, done, _chore("9", complete=False, start=date(2026, 11, 21))],
        groups=groups,
    )
    service, unifi, _metrics = _service(tmp_path, api, enabled=True)
    tasks = await service.skylight.tasks_for(DAY)
    assert sorted((task.id, task.complete) for task in tasks) == [("1", False), ("2", True)]

    await service.poll()
    _assert_chicago_day(api)
    assert unifi.writes() == []
    assert unifi.objects[0]["enabled"] is True
    assert "tasks=1/2" in caplog.text
    assert "unlock=False" in caplog.text
    assert "lock=True" in caplog.text
    assert "required tasks remain" in caplog.text


async def test_no_chores_keeps_empty_day_behavior(tmp_path, caplog):
    caplog.set_level("INFO")
    blocked, blocked_api, _metrics = _service(
        tmp_path, FakeSkylight(), enabled=False, latch="blocked.json"
    )
    await blocked.poll()
    assert blocked_api.writes()[0]["body"]["enabled"] is True
    assert "tasks=0/0" in caplog.text
    assert "unlock=False" in caplog.text
    assert "no required tasks" in caplog.text

    caplog.clear()
    opened, opened_api, _metrics = _service(
        tmp_path,
        FakeSkylight(),
        enabled=True,
        latch="opened.json",
        empty_day_unlocks=True,
    )
    await opened.poll()
    assert opened_api.writes()[0]["body"]["enabled"] is False
    assert "tasks=0/0" in caplog.text
    assert "unlock=True" in caplog.text
    assert "no required tasks" in caplog.text


@pytest.mark.parametrize(
    "error_on", ["get_frames", "get_categories", "get_all_chores", "get_chores"]
)
async def test_fetch_error_stays_blocked(tmp_path, caplog, error_on):
    caplog.set_level("INFO")
    api = FakeSkylight(
        dated=[_chore("1", complete=True), _chore("2", complete=True)],
        error=RuntimeError(PASSWORD),
        error_on=error_on,
    )
    service, unifi, metrics = _service(tmp_path, api, enabled=True, empty_day_unlocks=True)
    await service.poll()
    assert unifi.writes() == []
    assert unifi.objects[0]["enabled"] is True
    assert "skylight_unreachable" in caplog.text
    assert "unlock=False" in caplog.text
    assert "lock=False" in caplog.text
    assert "no required tasks" not in caplog.text
    assert PASSWORD not in caplog.text
    assert API_KEY not in caplog.text
    text = generate_latest(metrics._registry).decode()
    assert "wifi_automation_poll_failures_total 1.0" in text


async def test_late_and_routine_buckets_from_all_chores_still_count(tmp_path, caplog):
    caplog.set_level("INFO")
    late = _chore("1", complete=False, start=date(2026, 11, 18), summary="Yesterday")
    routine = _chore("2", complete=False, routine=True, summary="Brush teeth")
    groups = SimpleNamespace(chores={"late": [late]}, routines={"today": [routine]})
    api = FakeSkylight(groups=groups)
    service, unifi, _metrics = _service(tmp_path, api, enabled=True)
    tasks = await service.skylight.tasks_for(DAY)
    by_id = {task.id: task for task in tasks}
    assert by_id["1"].late is True and by_id["1"].complete is False
    assert by_id["2"].routine is True and by_id["2"].complete is False
    await service.poll()
    assert unifi.writes() == []
    assert "tasks=0/2" in caplog.text
    assert "unlock=False" in caplog.text


async def test_completed_routine_keeps_the_routine_flag(tmp_path, caplog):
    caplog.set_level("INFO")
    routine = _chore("1", complete=True, routine=True, summary="Brush teeth")
    ignored, ignored_api, _metrics = _service(
        tmp_path,
        FakeSkylight(dated=[routine]),
        enabled=True,
        include_routines=False,
        latch="ignored.json",
    )
    await ignored.poll()
    assert ignored_api.writes() == []
    assert "tasks=0/0" in caplog.text
    assert "no required tasks" in caplog.text

    caplog.clear()
    counted, counted_api, _metrics = _service(
        tmp_path,
        FakeSkylight(dated=[routine]),
        enabled=True,
        latch="counted.json",
    )
    await counted.poll()
    assert counted_api.writes()[0]["body"]["enabled"] is False
    assert "tasks=1/1" in caplog.text
    assert "unlock=True" in caplog.text
