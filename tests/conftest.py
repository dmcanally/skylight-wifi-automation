from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from skylight_wifi_automation.config import AppConfig, ChildConfig

PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
API_KEY = "test-api-key-do-not-log"
PASSWORD = "test-skylight-password-do-not-log"
MAC = "02:00:00:00:00:01"
CLIENT_IP = "203.0.113.50"
CHICAGO = ZoneInfo("America/Chicago")


def chicago(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=CHICAGO)


def child(
    name: str = "Lanie",
    cutoff: str = "20:30",
    *,
    manual_unblock: bool = False,
    object_name: str | None = None,
) -> ChildConfig:
    return ChildConfig(
        name=name,
        skylight_profile=name,
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
