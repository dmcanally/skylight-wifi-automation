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

