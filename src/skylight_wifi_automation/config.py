from __future__ import annotations

from datetime import time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml
from cryptography.x509 import load_pem_x509_certificate
from pydantic import BaseModel, Field, field_validator, model_validator


class ChildConfig(BaseModel):
    name: str
    skylight_profile: str
    object_name: str
    cutoff: time
    # File-mode compatibility. Database mode migrates a true value into an
    # override and does not store the flag. Old YAML that contains it still loads.
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
        cleaned = value.strip()
        lowered = cleaned.casefold()
        if "paste your console certificate" in lowered or "<paste" in lowered:
            raise ValueError("unifi_tls_ca is a placeholder, not a certificate")
        if "-----BEGIN CERTIFICATE-----" not in cleaned or "-----END CERTIFICATE-----" not in cleaned:
            raise ValueError("unifi_tls_ca must contain a PEM certificate")
        try:
            load_pem_x509_certificate(cleaned.encode())
        except Exception:  # noqa: BLE001 - parser errors must not include the certificate
            raise ValueError("unifi_tls_ca is not a parseable X.509 certificate") from None
        return cleaned

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
