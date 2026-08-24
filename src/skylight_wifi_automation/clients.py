from __future__ import annotations

import inspect
from collections.abc import Iterable
from datetime import date
from typing import Any, Self

import aiohttp
from googlewifi import GoogleWifi
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
        profile_by_id = {str(category.id): str(_value(category, "label", "")) for category in categories}
        groups = await self._client.get_all_chores(frame_id)
        today = day.isoformat()
        output: list[Task] = []
        for bucket, items in _task_buckets(groups):
            for chore in items:
                start = str(_value(chore, "start", ""))
                if bucket != "late" and start and start != today:
                    continue
                relationship = _value(chore, "category_id")
                if relationship is None:
                    raw = getattr(chore, "raw", {}) or {}
                    relationship = (
                        raw.get("relationships", {}).get("category", {}).get("data", {}).get("id")
                    )
                status = str(_value(chore, "status", "")).casefold()
                completed_on = _value(chore, "completed_on")
                output.append(
                    Task(
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
                )
        return output


def _task_buckets(groups: Any) -> Iterable[tuple[str, list[Any]]]:
    for collection_name in ("chores", "routines"):
        collection = getattr(groups, collection_name, {}) or {}
        for bucket, items in collection.items():
            if bucket == "late" or "today" in bucket:
                yield ("routine:" if collection_name == "routines" else "") + bucket, items


class GoogleWifiClient:
    def __init__(self, refresh_token: str) -> None:
        self._session = aiohttp.ClientSession()
        self._client = GoogleWifi(refresh_token, session=self._session)
        self._system_id: str | None = None
        self._devices: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        await self._session.close()

    async def refresh(self) -> dict[str, dict[str, Any]]:
        systems = await self._client.get_systems()
        if len(systems) != 1:
            raise RuntimeError(f"Expected one Google Wi-Fi system, found {len(systems)}")
        self._system_id, system = next(iter(systems.items()))
        self._devices = system.get("devices", {})
        return self._devices

    async def set_internet(self, device_ids: list[str], enabled: bool, dry_run: bool) -> None:
        if self._system_id is None:
            await self.refresh()
        missing = sorted(set(device_ids) - set(self._devices))
        if missing:
            raise RuntimeError(f"Unknown Google Wi-Fi device IDs: {', '.join(missing)}")
        if dry_run:
            return
        assert self._system_id is not None
        for device_id in device_ids:
            current_paused = bool(self._devices[device_id].get("paused"))
            desired_paused = not enabled
            if current_paused == desired_paused:
                continue
            result = self._client.pause_device(self._system_id, device_id, desired_paused)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                raise RuntimeError(f"Google Wi-Fi rejected state change for device {device_id}")
            self._devices[device_id]["paused"] = desired_paused

    def discovery_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "id": device_id,
                "name": data.get("friendlyName") or data.get("name") or "Unknown",
                "paused": bool(data.get("paused")),
            }
            for device_id, data in sorted(self._devices.items())
        ]
