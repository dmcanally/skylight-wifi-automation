from datetime import time

import pytest
from pydantic import ValidationError
from tests.conftest import PEM

from skylight_wifi_automation.config import AppConfig, load_config


def test_example_matches_the_deploy_contract():
    config = load_config("config.example.yaml")
    assert config.timezone == "America/Chicago"
    assert config.dry_run is True
    assert config.empty_day_unlocks is False
    assert config.unifi_url == "https://unifi.local/proxy/network/integration/v1"
    assert config.unifi_site is None
    assert [child.name for child in config.children] == ["child-a", "child-b", "child-c"]
    assert [child.skylight_profile for child in config.children] == [
        "Child A",
        "Child B",
        "Child C",
    ]
    assert [child.object_name for child in config.children] == [
        "No Internet - child-a",
        "No Internet - child-b",
        "No Internet - child-c",
    ]
    assert config.children[0].cutoff == time(20, 30)
    assert config.children[2].cutoff == time(20, 0)
    assert all(child.manual_unblock is False for child in config.children)
    assert "<paste your console certificate>" in config.unifi_tls_ca
    assert "-----BEGIN CERTIFICATE-----" in config.unifi_tls_ca
    assert "-----END CERTIFICATE-----" in config.unifi_tls_ca


def test_manual_unblock_and_empty_day_default_off(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """unifi_url: https://unifi.local/proxy/network/integration/v1
unifi_tls_ca: |
  -----BEGIN CERTIFICATE-----
  MIIB
  -----END CERTIFICATE-----
children:
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
