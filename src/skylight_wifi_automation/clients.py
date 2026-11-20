from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any, Self

from pyskylight import PasswordAuth, Skylight

from .models import Task


def _value(item: Any, name: str, default: Any = None) -> Any:
    value = getattr(item, name, default)
    if value is not default:
        return value
    attributes = getattr(item, "attributes", {}) or {}
    return attributes.get(name, default)


class SkylightClient:
    def __init__(self, email: str, password: str) -> None:
        self._email = email
        self._password = password
        self._client: Skylight | None = None

    async def __aenter__(self) -> Self:
        self._client = Skylight(PasswordAuth(self._email, self._password))
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        assert self._client is not None
        await self._client.__aexit__(*args)

    async def tasks_for(self, day: date) -> list[Task]:
        assert self._client is not None
        frames = await self._client.get_frames()
        if len(frames) != 1:
            raise RuntimeError(f"Expected one Skylight frame, found {len(frames)}")
        frame_id = frames[0].id
        categories = await self._client.get_categories(frame_id)
        profile_by_id = {
            str(category.id): str(_value(category, "label", "")) for category in categories
        }
        # GET /chores/all is an open-work view: completed occurrences disappear,
        # so a finished day looks like no chores. The date-range list includes them.
        # `day` is the local date in the configured timezone.
        groups = await self._client.get_all_chores(frame_id)
        dated = await self._client.get_chores(frame_id, after=day, before=day)
        today = day.isoformat()
        found: dict[str, Task] = {}
        for bucket, items in _task_buckets(groups):
            for chore in items:
                start = _start_text(chore)
                if bucket != "late" and start and start != today:
                    continue
                _remember(found, _task_from_chore(chore, profile_by_id, bucket))
        for chore in dated:
            start = _start_text(chore)
            if start and start != today:
                continue
            _remember(found, _task_from_chore(chore, profile_by_id, "today"))
        return list(found.values())


def _start_text(chore: Any) -> str:
    value = _value(chore, "start", "")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def _task_from_chore(chore: Any, profile_by_id: dict[str, str], bucket: str) -> Task:
    relationship = _value(chore, "category_id")
    if relationship is None:
        raw = getattr(chore, "raw", {}) or {}
        relationship = raw.get("relationships", {}).get("category", {}).get("data", {}).get("id")
    status = str(_value(chore, "status", "")).casefold()
    completed_on = _value(chore, "completed_on")
    return Task(
        id=str(chore.id),
        profile=profile_by_id.get(str(relationship), ""),
        summary=str(_value(chore, "summary", "Unnamed task")),
        complete=bool(_value(chore, "completed", False))
        or status in {"complete", "completed"}
        or completed_on is not None,
        skipped=status == "skipped",
        routine=bool(_value(chore, "routine", False)) or "routine" in bucket,
        late=bucket == "late",
    )


def _remember(found: dict[str, Task], task: Task) -> None:
    previous = found.get(task.id)
    if previous is None:
        found[task.id] = task
        return
    incoming_done = task.complete or task.skipped
    previous_done = previous.complete or previous.skipped
    primary = task if incoming_done and not previous_done else previous
    other = previous if primary is task else task
    found[task.id] = Task(
        id=primary.id,
        profile=primary.profile or other.profile,
        summary=primary.summary,
        complete=primary.complete or other.complete,
        skipped=primary.skipped or other.skipped,
        routine=primary.routine or other.routine,
        late=primary.late or other.late,
    )


def _task_buckets(groups: Any) -> Iterable[tuple[str, list[Any]]]:
    for collection_name in ("chores", "routines"):
        collection = getattr(groups, collection_name, {}) or {}
        for bucket, items in collection.items():
            if bucket == "late" or "today" in bucket:
                yield ("routine:" if collection_name == "routines" else "") + bucket, items
