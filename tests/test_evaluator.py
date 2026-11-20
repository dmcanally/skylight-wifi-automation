from datetime import timedelta

from tests.conftest import app_config, chicago, child

from skylight_wifi_automation.evaluator import decide
from skylight_wifi_automation.models import Task


def task(**overrides):
    values = {
        "id": "1",
        "profile": "Child A",
        "summary": "Dishes",
        "complete": False,
        "skipped": False,
        "routine": False,
        "late": False,
    }
    values.update(overrides)
    return Task(**values)


def test_unlocks_when_all_tasks_complete_or_skipped():
    result = decide(
        child(),
        [task(complete=True), task(id="2", skipped=True)],
        chicago(2026, 8, 24, 18),
        app_config(),
        already_unlocked=False,
    )
    assert result.should_unlock is True
    assert result.should_lock is False
    assert (result.complete, result.required) == (2, 2)


def test_stays_blocked_when_a_task_remains():
    result = decide(
        child(), [task()], chicago(2026, 8, 24, 18), app_config(), already_unlocked=False
    )
    assert result.should_unlock is False
    assert result.should_lock is True


def test_empty_day_stays_blocked_by_default():
    result = decide(child(), [], chicago(2026, 8, 24, 18), app_config(), already_unlocked=False)
    assert result.should_unlock is False
    assert result.reason == "no required tasks"


def test_empty_day_can_unlock_when_configured():
    result = decide(
        child(),
        [],
        chicago(2026, 8, 24, 18),
        app_config(empty_day_unlocks=True),
        already_unlocked=False,
    )
    assert result.should_unlock is True


def test_cutoff_locks_even_when_tasks_are_complete():
    result = decide(
        child(),
        [task(complete=True)],
        chicago(2026, 8, 24, 20, 30),
        app_config(),
        already_unlocked=True,
    )
    assert result.should_unlock is False
    assert result.reason == "daily cutoff reached"


def test_manual_unblock_before_cutoff_ignores_open_tasks():
    result = decide(
        child(manual_unblock=True),
        [task()],
        chicago(2026, 8, 24, 18),
        app_config(),
        already_unlocked=False,
    )
    assert result.should_unlock is True
    assert result.reason == "manual unblock"


def test_manual_unblock_does_not_pass_cutoff():
    result = decide(
        child(manual_unblock=True),
        [task(complete=True)],
        chicago(2026, 8, 24, 20, 30),
        app_config(),
        already_unlocked=False,
    )
    assert result.should_unlock is False


def test_latch_keeps_access_when_a_task_is_unchecked():
    result = decide(
        child(),
        [task()],
        chicago(2026, 8, 24, 19),
        app_config(),
        already_unlocked=True,
    )
    assert result.should_unlock is True
    assert result.reason == "already unlocked today"


def test_other_profiles_do_not_block_the_child():
    result = decide(
        child(),
        [task(profile="Child B")],
        chicago(2026, 8, 24, 18),
        app_config(),
        already_unlocked=False,
    )
    assert result.should_unlock is False
    assert result.reason == "no required tasks"


def test_cutoffs_follow_chicago_wall_time_across_dst():
    winter = chicago(2026, 1, 15, 20, 30)
    summer = chicago(2026, 7, 15, 20, 30)
    assert winter.utcoffset() == timedelta(hours=-6)
    assert summer.utcoffset() == timedelta(hours=-5)
    config = app_config()
    kid = child()
    assert decide(kid, [task(complete=True)], winter, config, already_unlocked=False).should_unlock is False
    assert (
        decide(
            kid,
            [task(complete=True)],
            chicago(2026, 1, 15, 20, 29),
            config,
            already_unlocked=False,
        ).should_unlock
        is True
    )
    assert decide(kid, [task(complete=True)], summer, config, already_unlocked=False).should_unlock is False
    assert (
        decide(
            kid,
            [task(complete=True)],
            chicago(2026, 7, 15, 20, 29),
            config,
            already_unlocked=False,
        ).should_unlock
        is True
    )
    # 2026-11-01 is the fall-back day. 20:15 CST is after the 20:00 cutoff and before 20:30.
    moment = chicago(2026, 11, 1, 20, 15)
    assert moment.utcoffset() == timedelta(hours=-6)
    assert (
        decide(
            child("child-c", "20:00"),
            [task(profile="Child C", complete=True)],
            moment,
            config,
            already_unlocked=False,
        ).should_unlock
        is False
    )
    assert (
        decide(child(), [task(complete=True)], moment, config, already_unlocked=False).should_unlock
        is True
    )
