from __future__ import annotations

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class ChildConfig(BaseModel):
    name: str
    skylight_profile: str
    family_wifi_group: str
    cutoff: time


class AppConfig(BaseModel):
    timezone: str = "America/Chicago"
    poll_seconds: int = Field(default=60, ge=30, le=3600)
    dry_run: bool = True
    include_late: bool = True
    include_routines: bool = True
    empty_day_unlocks: bool = True
    children: list[ChildConfig]

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def unique_children(self) -> AppConfig:
        names = [child.name.casefold() for child in self.children]
        profiles = [child.skylight_profile.casefold() for child in self.children]
        groups = [child.family_wifi_group.casefold() for child in self.children]
        if (
            len(names) != len(set(names))
            or len(profiles) != len(set(profiles))
            or len(groups) != len(set(groups))
        ):
            raise ValueError("Child names, Skylight profiles, and Family Wi-Fi groups must be unique")
        return self


def load_config(path: str | Path) -> AppConfig:
    with Path(path).open(encoding="utf-8") as handle:
        return AppConfig.model_validate(yaml.safe_load(handle))
