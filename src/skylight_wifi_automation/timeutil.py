from __future__ import annotations

from datetime import UTC, datetime


def dump_dt(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetime")
    return value.astimezone(UTC).isoformat(timespec="seconds")


def load_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive datetime")
    return parsed.astimezone(UTC)
