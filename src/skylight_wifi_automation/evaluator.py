from __future__ import annotations

from datetime import datetime, time

from .config import AppConfig, ChildConfig
from .models import ChildDecision, Task


def past_cutoff(cutoff: time, now: datetime) -> bool:
    """True at or after the child's local cutoff.

    ``now`` must already be in the configured timezone. Wall-clock comparison
    follows that zone, America/Chicago by default, through its standard and
    daylight offsets.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.timetz().replace(tzinfo=None) >= cutoff


def decide(
    child: ChildConfig,
    tasks: list[Task],
    now: datetime,
    config: AppConfig,
    *,
    already_unlocked: bool,
) -> ChildDecision:
    relevant = [
        task
        for task in tasks
        if task.profile.casefold() == child.skylight_profile.casefold()
        and (config.include_routines or not task.routine)
        and (config.include_late or not task.late)
    ]
    completed = sum(task.complete or task.skipped for task in relevant)
    required = len(relevant)
    if past_cutoff(child.cutoff, now):
        return ChildDecision(child.name, False, "daily cutoff reached", completed, required)
    if child.manual_unblock:
        return ChildDecision(child.name, True, "manual unblock", completed, required)
    if already_unlocked:
        return ChildDecision(child.name, True, "already unlocked today", completed, required)
    if not relevant:
        return ChildDecision(
            child.name,
            config.empty_day_unlocks,
            "no required tasks",
            0,
            0,
        )
    if completed == required:
        return ChildDecision(
            child.name, True, "all required tasks complete", completed, required
        )
    return ChildDecision(child.name, False, "required tasks remain", completed, required)
