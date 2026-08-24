from datetime import datetime
from zoneinfo import ZoneInfo

from skylight_wifi_automation.config import AppConfig, ChildConfig
from skylight_wifi_automation.evaluator import decide
from skylight_wifi_automation.models import Task


def app_config(**overrides):
    values = {
        "children": [],
        "empty_day_unlocks": True,
        "include_late": True,
        "include_routines": True,
    }
    values.update(overrides)
    return AppConfig(**values)


def child(cutoff="21:00"):
    return ChildConfig(
        name="Lanie",
        skylight_profile="Lanie",
        cutoff=cutoff,
        expected_device_count=4,
        google_device_ids=[],
    )


def at(hour, minute=0):
    return datetime(2026, 8, 24, hour, minute, tzinfo=ZoneInfo("America/Chicago"))


def task(**overrides):
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


def test_unlocks_when_all_tasks_complete():
    result = decide(child(), [task(complete=True), task(id="2", skipped=True)], at(18), app_config())
    assert result.should_unlock is True
    assert (result.complete, result.required) == (2, 2)


def test_stays_paused_when_a_task_remains():
    result = decide(child(), [task()], at(18), app_config())
    assert result.should_unlock is False


def test_empty_day_unlocks():
    result = decide(child(), [], at(18), app_config())
    assert result.should_unlock is True


def test_cutoff_always_pauses():
    result = decide(child(), [task(complete=True)], at(21), app_config())
    assert result.should_unlock is False
    assert result.reason == "daily cutoff reached"


def test_other_profiles_do_not_block_child():
    result = decide(child(), [task(profile="Libby")], at(18), app_config())
    assert result.should_unlock is True

