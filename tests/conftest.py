from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from skylight_wifi_automation.config import AppConfig, ChildConfig

PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
API_KEY = "test-api-key-do-not-log"
PASSWORD = "test-skylight-password-do-not-log"
# RFC 7042 documentation MAC, RFC 5737 documentation address, generic device name.
MAC = "00:00:5e:00:53:01"
CLIENT_IP = "192.0.2.50"
DEVICE_NAME = "tablet-1"
CHICAGO = ZoneInfo("America/Chicago")

_PROFILES = {
    "child-a": "Child A",
    "child-b": "Child B",
    "child-c": "Child C",
}


def chicago(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=CHICAGO)


def child(
    name: str = "child-a",
    cutoff: str = "20:30",
    *,
    manual_unblock: bool = False,
    object_name: str | None = None,
    profile: str | None = None,
) -> ChildConfig:
    return ChildConfig(
        name=name,
        skylight_profile=profile if profile is not None else _PROFILES.get(name, name),
        object_name=object_name or f"No Internet - {name}",
        cutoff=cutoff,
        manual_unblock=manual_unblock,
    )


def app_config(*children: ChildConfig, **overrides) -> AppConfig:
    values = {
        "unifi_url": "https://unifi.local/proxy/network/integration/v1",
        "unifi_tls_ca": PEM,
        "empty_day_unlocks": False,
        "dry_run": False,
        "children": list(children) or [child()],
    }
    values.update(overrides)
    return AppConfig(**values)
