from datetime import time
from pathlib import Path
from textwrap import indent

import pytest
import yaml
from pydantic import ValidationError
from tests.conftest import PEM

from skylight_wifi_automation.config import AppConfig, load_config


def _child(name: str, cutoff: str, manual: bool = False) -> dict[str, object]:
    profiles = {"child-a": "Child A", "child-b": "Child B", "child-c": "Child C"}
    return {
        "name": name,
        "skylight_profile": profiles[name],
        "object_name": f"No Internet - {name}",
        "cutoff": cutoff,
        "manual_unblock": manual,
    }


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "unifi_url": "https://unifi.local/proxy/network/integration/v1",
        "unifi_tls_ca": PEM,
        "children": [_child("child-a", "20:30")],
    }
    payload.update(overrides)
    return payload


def test_example_matches_the_deploy_contract():
    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    assert raw["timezone"] == "America/Chicago"
    assert raw["dry_run"] is True
    assert raw["empty_day_unlocks"] is False
    assert raw["unifi_url"] == "https://unifi.local/proxy/network/integration/v1"
    assert "unifi_site" not in raw or raw["unifi_site"] is None
    assert [child["name"] for child in raw["children"]] == ["child-a", "child-b", "child-c"]
    assert [child["skylight_profile"] for child in raw["children"]] == [
        "Child A",
        "Child B",
        "Child C",
    ]
    assert [child["object_name"] for child in raw["children"]] == [
        "No Internet - child-a",
        "No Internet - child-b",
        "No Internet - child-c",
    ]
    assert raw["children"][0]["cutoff"] == "20:30"
    assert raw["children"][2]["cutoff"] == "20:00"
    assert all(child["manual_unblock"] is False for child in raw["children"])
    assert "<paste your console certificate>" in raw["unifi_tls_ca"]
    with pytest.raises(ValidationError) as caught:
        load_config("config.example.yaml")
    assert any("unifi_tls_ca" in err["loc"] for err in caught.value.errors())


def test_placeholder_and_unparseable_certificate_are_rejected():
    garbage = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    for certificate in (
        PEM + "\n<paste your console certificate>\n",
        garbage,
        "not a certificate",
    ):
        with pytest.raises(ValidationError):
            AppConfig.model_validate(_payload(unifi_tls_ca=certificate))
    config = AppConfig.model_validate(_payload())
    assert "BEGIN CERTIFICATE" in config.unifi_tls_ca
    assert config.children[0].cutoff == time(20, 30)


def test_manual_unblock_and_empty_day_default_off(tmp_path):
    path = tmp_path / "config.yaml"
    pem = indent(PEM.strip() + "\n", "  ")
    path.write_text(
        f"""unifi_url: https://unifi.local/proxy/network/integration/v1
unifi_tls_ca: |
{pem}children:
  - name: child-a
    skylight_profile: Child A
    object_name: No Internet - child-a
    cutoff: '20:30'
    manual_unblock:
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.empty_day_unlocks is False
    assert config.dry_run is True
    assert config.children[0].manual_unblock is False
    assert "BEGIN CERTIFICATE" in config.unifi_tls_ca


def test_unifi_url_must_be_https():
    with pytest.raises(ValidationError):
        load_config_text = {
            "unifi_url": "http://unifi.local/proxy/network/integration/v1",
            "unifi_tls_ca": PEM,
            "children": [
                {
                    "name": "child-a",
                    "skylight_profile": "Child A",
                    "object_name": "No Internet - child-a",
                    "cutoff": "20:30",
                }
            ],
        }
        AppConfig.model_validate(load_config_text)
