from __future__ import annotations

from datetime import datetime

from .config import AppConfig, ChildConfig
from .models import ChildDecision, Task


def decide(
    child: ChildConfig, tasks: list[Task], now: datetime, config: AppConfig
) -> ChildDecision:
    if now.timetz().replace(tzinfo=None) >= child.cutoff:
        return ChildDecision(child.name, False, "daily cutoff reached", 0, 0)

    relevant = [
        task
        for task in tasks
        if task.profile.casefold() == child.skylight_profile.casefold()
        and (config.include_routines or not task.routine)
        and (config.include_late or not task.late)
    ]
    completed = sum(task.complete or task.skipped for task in relevant)
    if not relevant:
        return ChildDecision(
            child.name,
            config.empty_day_unlocks,
            "no required tasks",
            0,
            0,
        )
    if completed == len(relevant):
        return ChildDecision(
            child.name, True, "all required tasks complete", completed, len(relevant)
        )
    return ChildDecision(
        child.name, False, "required tasks remain", completed, len(relevant)
    )

