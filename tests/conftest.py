from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from skylight_wifi_automation.config import AppConfig, ChildConfig

# Generated for tests. CN=unifi.local. Not a real console certificate.
PEM = """-----BEGIN CERTIFICATE-----
MIIDJTCCAg2gAwIBAgIUVg9y3BtgsHUWZO5A3LXZ3wkCjb8wDQYJKoZIhvcNAQEL
BQAwFjEUMBIGA1UEAwwLdW5pZmkubG9jYWwwHhcNMjYxMTIwMTU1MDA4WhcNMzYx
MTE3MTU1MDA4WjAWMRQwEgYDVQQDDAt1bmlmaS5sb2NhbDCCASIwDQYJKoZIhvcN
AQEBBQADggEPADCCAQoCggEBAO/BPJGrl++em0MEOfBNZJyXk+2MqXNPAee9gi1L
gL1ZJmRVwnQc7d+kXoopi5fySGYwOICb+WoBCRLfzVTbw5HCB4s+Xh6OxUwLFX2O
an3BJgiwSQaj7xW0tdMYj1ab9dtPO4W1UhATL1yi3fiAE4fXl9Dtr2gPMIBXR2fN
4OLds9kWrR1NIZeQH2QdX5n4jAzaL2O19ex7+fr7gDj6IlU5hb3svj6VWPCKWprq
wTAF7cDPXP2j8Mulwnm/kHVv+yaxukG+ZayfaF/JzO14e/BuOpmZbOkp2Czes6o0
WON2ngj60wPrjlxZ3MHJZqzVEL+i1acmQxFXSULrQRVubq0CAwEAAaNrMGkwHQYD
VR0OBBYEFGgtNf3VXHBGd1QakjizNrFLy/RzMB8GA1UdIwQYMBaAFGgtNf3VXHBG
d1QakjizNrFLy/RzMA8GA1UdEwEB/wQFMAMBAf8wFgYDVR0RBA8wDYILdW5pZmku
bG9jYWwwDQYJKoZIhvcNAQELBQADggEBAL4JqntTLPJKp7vHuk//ytd6Tcs4tqVm
1NCS2697ettnDbuOdqAGQEtWE3XLNcmtCnG/cG23xUtXlRB1mPpcAvaW984YBEHf
6y+8T6+2VZQfcI/YLpZk40xyAALZB1IMsNPcJRAuJCcT9Q9m1AGBQv4rd+fG8b5w
hIuPIZwZWx5Q6bAGC9Aobtf7YOMIMN8XGqVyK9lhuB9BGIPbbwbrGrKNhepAweAN
7JSu+efinkf2Q9Bp4St7iXC2s8nnqPgOauFqO7Ep2Rx0I8mxAWoX37XxGxawtSD+
td4azV/zSReDBSbGl7Nsh9QmRMmBosCuugxjtEF9fM/tpvMGtTCu3HE=
-----END CERTIFICATE-----
"""
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
