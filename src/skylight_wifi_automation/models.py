from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    id: str
    profile: str
    summary: str
    complete: bool
    skipped: bool
    routine: bool
    late: bool


@dataclass(frozen=True)
class ChildDecision:
    child: str
    should_unlock: bool
    reason: str
    complete: int
    required: int

    @property
    def should_lock(self) -> bool:
        return not self.should_unlock


@dataclass(frozen=True)
class ChildPoll:
    """Safe dashboard fields from the latest poll. No secrets and no object body."""

    name: str
    complete: int
    required: int
    blocked: bool | None
    reason: str
    action: str
    write: bool
