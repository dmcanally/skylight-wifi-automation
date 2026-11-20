from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import ChildDecision

UNBLOCK_CONFIRMATION = "UNBLOCK"


class OverrideError(ValueError):
    """A safe message for the operator. It never includes a secret."""


class ConfirmationRequired(OverrideError):
    def __init__(self, expires_at: datetime) -> None:
        super().__init__("Type UNBLOCK to confirm this unblock.")
        self.expires_at = expires_at


@dataclass(frozen=True)
class ActiveOverride:
    id: int
    child_id: int
    child: str
    action: str
    expires_at: datetime
    set_by: str
    created_at: datetime


def apply_override(decision: ChildDecision, action: str) -> ChildDecision:
    if action == "unblock":
        return ChildDecision(
            decision.child, True, "manual unblock", decision.complete, decision.required
        )
    if action == "block":
        return ChildDecision(
            decision.child, False, "manual block", decision.complete, decision.required
        )
    raise OverrideError("Unknown action.")


def override_expiry(now: datetime, cutoff: time, timezone: str) -> datetime:
    """Earliest of the next cutoff and the next local midnight, as UTC.

    An override set after today's cutoff therefore lasts until local midnight,
    not until tomorrow's cutoff. A cutoff of 00:00 is the same instant as midnight.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    expiry = min(_next_cutoff(local, cutoff, zone), _next_midnight(local, zone))
    return expiry.astimezone(UTC)


def _next_cutoff(local_now: datetime, cutoff: time, zone: ZoneInfo) -> datetime:
    candidate = _at(local_now.date(), cutoff, zone)
    if candidate < local_now:
        candidate = _at(local_now.date() + timedelta(days=1), cutoff, zone)
    return candidate


def _next_midnight(local_now: datetime, zone: ZoneInfo) -> datetime:
    return _at(local_now.date() + timedelta(days=1), time(0, 0), zone)


def _at(day: date, clock: time, zone: ZoneInfo) -> datetime:
    local = datetime.combine(day, clock, tzinfo=zone)
    # A spring-forward gap is not a real civil time. The UTC round-trip lands
    # on the post-transition instant. A fall-back fold keeps the first one.
    normalized = local.astimezone(UTC).astimezone(zone)
    if (normalized.hour, normalized.minute, normalized.second) != (
        clock.hour,
        clock.minute,
        clock.second,
    ):
        return normalized
    if normalized.utcoffset() != local.utcoffset():
        return local
    return normalized
