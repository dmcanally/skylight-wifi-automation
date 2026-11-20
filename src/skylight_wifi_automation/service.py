from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig, ChildConfig
from .evaluator import decide, past_cutoff
from .metrics import Metrics
from .models import ChildDecision, ChildPoll, Task
from .overrides import apply_override
from .unifi import NetworkObject, UniFiError

LOGGER = logging.getLogger(__name__)

LATCH_PATH = Path(os.environ.get("LATCH_PATH", "/tmp/skylight-wifi-latch.json"))


class PollError(RuntimeError):
    """A poll could not be fully applied. The message is safe to log."""


class AutomationService:
    def __init__(
        self,
        config: AppConfig,
        skylight: object,
        unifi: object,
        metrics: Metrics,
        *,
        clock: Callable[[], datetime] | None = None,
        latch_path: Path | None = None,
        reloader: Callable[[AutomationService], Awaitable[None]] | None = None,
        overrides: object | None = None,
    ) -> None:
        self.config = config
        self.skylight = skylight
        self.unifi = unifi
        self.metrics = metrics
        self._clock = clock or _now(config.timezone)
        self._latch_path = latch_path if latch_path is not None else LATCH_PATH
        self._reloader = reloader
        self._overrides = overrides
        self.unlocked_on: dict[str, date] = _load_latch(self._latch_path)
        self.last_poll: dict[str, ChildPoll] = {}
        self.last_poll_at: datetime | None = None

    async def poll(self) -> None:
        try:
            await self.run_once()
        except Exception:  # noqa: BLE001 - every poll error must be counted, then the loop continues
            self.metrics.record_failure()
            LOGGER.error("poll failed; object writes stopped")
            return
        self.metrics.record_success()

    async def _reload(self) -> None:
        reloader = self._reloader
        if reloader is not None:
            await reloader(self)

    async def _expire_overrides(self, now: datetime) -> None:
        source = self._overrides
        if source is None:
            return
        expired = await source.expire_due(now)  # type: ignore[attr-defined]
        for item in expired:
            LOGGER.info("override expired child=%s action=%s", item.child, item.action)

    async def _active_override(self, label: str, now: datetime) -> object | None:
        source = self._overrides
        if source is None:
            return None
        return await source.active_override(label, now)  # type: ignore[attr-defined]

    async def run_once(self) -> None:
        await self._reload()
        now = self._clock().astimezone(ZoneInfo(self.config.timezone))
        today = now.date()
        self.last_poll = {}
        self.last_poll_at = now
        self.unlocked_on = {
            name: day for name, day in self.unlocked_on.items() if day == today
        }
        await self._expire_overrides(now)

        skylight_error = False
        tasks: list[Task] = []
        try:
            tasks = await _tasks_for(self.skylight, today)
        except Exception:  # noqa: BLE001 - Skylight's client raises library-specific errors
            skylight_error = True
            LOGGER.error("skylight request failed; non-cutoff state will stay unchanged")

        try:
            listed = await _list_objects(self.unifi)
        except Exception as exc:  # noqa: BLE001 - a UniFi failure must not change object state
            LOGGER.error(
                "unifi request failed; leaving object state unchanged detail=%s",
                _safe_detail(exc),
            )
            for child in self.config.children:
                _log_line(
                    child,
                    None,
                    self.config.dry_run,
                    "unchanged",
                    False,
                    "unifi_unreachable",
                    status=self.last_poll,
                )
            raise PollError("unifi request failed") from None

        allowed = frozenset(child.object_name for child in self.config.children)
        name_error = False
        for child in self.config.children:
            matches = [obj for obj in listed if obj.name == child.object_name]
            past = past_cutoff(child.cutoff, now)
            if len(matches) != 1:
                name_error = True
                LOGGER.error(
                    "unifi object name match child=%s count=%s", child.name, len(matches)
                )
                reason = "object_name_not_found" if not matches else "object_name_not_unique"
                _log_line(
                    child, None, self.config.dry_run, "unchanged", False, reason, status=self.last_poll
                )
                continue
            override = await self._active_override(child.name, now)
            # A Skylight failure is fail-closed unless an override already says
            # what to do, or the cutoff itself is locking the child.
            if skylight_error and override is not None:
                base = ChildDecision(child.name, False, "skylight_unreachable", 0, 0)
            elif skylight_error and not past:
                _log_line(
                    child,
                    None,
                    self.config.dry_run,
                    "unchanged",
                    False,
                    "skylight_unreachable",
                    status=self.last_poll,
                )
                continue
            else:
                already = self.unlocked_on.get(child.name) == today
                base = decide(child, tasks, now, self.config, already_unlocked=already)
                if base.should_unlock:
                    self.unlocked_on[child.name] = today
                    _save_latch(self._latch_path, self.unlocked_on)
            decision = apply_override(base, override.action) if override is not None else base
            obj = matches[0]
            desired_enabled = not decision.should_unlock
            if obj.enabled == desired_enabled:
                _log_line(
                    child,
                    decision,
                    self.config.dry_run,
                    "unchanged",
                    False,
                    decision.reason,
                    status=self.last_poll,
                )
                continue
            action = "pause" if decision.should_unlock else "resume"
            if self.config.dry_run:
                _log_line(
                    child, decision, True, action, False, decision.reason, status=self.last_poll
                )
                continue
            try:
                await _set_blocked(self.unifi, obj, desired_enabled, allowed)
            except Exception as exc:  # noqa: BLE001 - a failed write stops the rest of the poll
                LOGGER.error(
                    "unifi write failed child=%s action=%s detail=%s",
                    child.name,
                    action,
                    _safe_detail(exc),
                )
                _log_line(
                    child,
                    decision,
                    False,
                    action,
                    False,
                    "unifi_write_failed",
                    status=self.last_poll,
                )
                raise PollError("unifi write failed") from None
            _log_line(
                child, decision, False, action, True, decision.reason, status=self.last_poll
            )

        if name_error:
            raise PollError("unifi object name did not match exactly one object")
        if skylight_error:
            raise PollError("skylight request failed")

    async def run_forever(self) -> None:
        while True:
            await self.poll()
            await asyncio.sleep(self.config.poll_seconds)


def _now(timezone: str) -> Callable[[], datetime]:
    def clock() -> datetime:
        return datetime.now(ZoneInfo(timezone))

    return clock


async def _tasks_for(skylight: object, day: date) -> list[Task]:
    method: Callable[[date], Awaitable[list[Task]]] = skylight.tasks_for  # type: ignore[attr-defined]
    return await method(day)


async def _list_objects(unifi: object) -> list[NetworkObject]:
    method: Callable[[], Awaitable[list[NetworkObject]]] = (
        unifi.list_objects  # type: ignore[attr-defined]
    )
    return await method()


async def _set_blocked(
    unifi: object, obj: NetworkObject, blocked: bool, allowed_names: frozenset[str]
) -> None:
    method: Callable[[NetworkObject, bool, frozenset[str]], Awaitable[None]] = (
        unifi.set_blocked  # type: ignore[attr-defined]
    )
    await method(obj, blocked, allowed_names)


def _safe_detail(exc: BaseException) -> str:
    if isinstance(exc, UniFiError):
        return str(exc)
    return type(exc).__name__


def _load_latch(path: Path) -> dict[str, date]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        LOGGER.error("latch file ignored")
        return {}
    if not isinstance(raw, dict):
        LOGGER.error("latch file ignored")
        return {}
    loaded: dict[str, date] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        try:
            loaded[name] = date.fromisoformat(value)
        except ValueError:
            continue
    return loaded


def _save_latch(path: Path, unlocked_on: dict[str, date]) -> None:
    payload = {name: day.isoformat() for name, day in sorted(unlocked_on.items())}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    except OSError:
        LOGGER.error("latch file was not saved")


def _log_line(
    child: ChildConfig,
    decision: ChildDecision | None,
    dry_run: bool,
    action: str,
    write: bool,
    reason: str,
    status: dict[str, ChildPoll] | None = None,
) -> None:
    if decision is None:
        unlock = False
        lock = False
        complete = 0
        required = 0
    else:
        unlock = decision.should_unlock
        lock = decision.should_lock
        complete = decision.complete
        required = decision.required
    LOGGER.info(
        "child=%s object=%s unlock=%s lock=%s tasks=%d/%d reason=%s dry_run=%s action=%s write=%s",
        child.name,
        child.object_name,
        unlock,
        lock,
        complete,
        required,
        reason,
        dry_run,
        action,
        write,
    )
    if status is not None:
        if decision is None:
            status[child.name] = ChildPoll(child.name, 0, 0, None, reason, action, write)
        else:
            status[child.name] = ChildPoll(
                child.name,
                decision.complete,
                decision.required,
                not decision.should_unlock,
                reason,
                action,
                write,
            )

