from datetime import time

import pytest
from pydantic import ValidationError
from tests.conftest import PEM

from skylight_wifi_automation.config import AppConfig, load_config
from skylight_wifi_automation.tls import build_ssl_context


def test_example_matches_the_deploy_contract():
    config = load_config("config.example.yaml")
    assert config.timezone == "America/Chicago"
    assert config.dry_run is True
    assert config.empty_day_unlocks is False
    assert config.unifi_url == "https://unifi.local/proxy/network/integration/v1"
    assert config.unifi_site is None
    assert [child.name for child in config.children] == ["Lanie", "Libby", "Tucker"]
    assert [child.object_name for child in config.children] == [
        "No Internet - Lanie",
        "No Internet - Libby",
        "No Internet - Tucker",
    ]
    assert config.children[0].cutoff == time(20, 30)
    assert config.children[2].cutoff == time(20, 0)
    assert all(child.manual_unblock is False for child in config.children)
    context = build_ssl_context(config.unifi_tls_ca)
    assert context.check_hostname is True


def test_manual_unblock_and_empty_day_default_off(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """unifi_url: https://unifi.local/proxy/network/integration/v1
unifi_tls_ca: |
  -----BEGIN CERTIFICATE-----
  MIIB
  -----END CERTIFICATE-----
children:
  - name: Lanie
    skylight_profile: Lanie
    object_name: No Internet - Lanie
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
                    "name": "Lanie",
                    "skylight_profile": "Lanie",
                    "object_name": "No Internet - Lanie",
                    "cutoff": "20:30",
                }
            ],
        }
        AppConfig.model_validate(load_config_text)
