from __future__ import annotations

from datetime import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class ChildConfig(BaseModel):
    name: str
    skylight_profile: str
    object_name: str
    cutoff: time
    manual_unblock: bool = False

    @field_validator("manual_unblock", mode="before")
    @classmethod
    def blank_manual_unblock(cls, value: object) -> object:
        if value is None or value == "":
            return False
        return value


class AppConfig(BaseModel):
    timezone: str = "America/Chicago"
    poll_seconds: int = Field(default=60, ge=30, le=3600)
    dry_run: bool = True
    include_late: bool = True
    include_routines: bool = True
    empty_day_unlocks: bool = False
    unifi_url: str
    unifi_site: str | None = None
    unifi_tls_ca: str
    children: list[ChildConfig] = Field(min_length=1)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("unifi_site", mode="before")
    @classmethod
    def blank_site(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("unifi_url")
    @classmethod
    def https_unifi_url(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("unifi_url must be an https URL with a hostname")
        return value.strip().rstrip("/")

    @field_validator("unifi_tls_ca")
    @classmethod
    def pem_certificate(cls, value: str) -> str:
        if "-----BEGIN CERTIFICATE-----" not in value or "-----END CERTIFICATE-----" not in value:
            raise ValueError("unifi_tls_ca must contain a PEM certificate")
        return value

    @model_validator(mode="after")
    def unique_children(self) -> AppConfig:
        names = [child.name.casefold() for child in self.children]
        profiles = [child.skylight_profile.casefold() for child in self.children]
        objects = [child.object_name.casefold() for child in self.children]
        if len(names) != len(set(names)) or len(profiles) != len(set(profiles)):
            raise ValueError("Child names and Skylight profiles must be unique")
        if len(objects) != len(set(objects)):
            raise ValueError("Object Manager object names must be unique")
        return self


def load_config(path: str | Path) -> AppConfig:
    with Path(path).open(encoding="utf-8") as handle:
        return AppConfig.model_validate(yaml.safe_load(handle))
