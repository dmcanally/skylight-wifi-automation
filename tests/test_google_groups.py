from skylight_wifi_automation.clients import extract_family_groups


def test_extracts_family_group_station_ids():
    payload = {
        "stationGroups": [
            {"groupName": "Lanie", "stationIds": ["one", "two"]},
            {
                "displayName": "Libby",
                "members": [{"stationId": "three"}, {"stationId": "four"}],
            },
        ]
    }
    assert extract_family_groups(payload) == {
        "Lanie": ["one", "two"],
        "Libby": ["three", "four"],
    }


def test_ignores_named_objects_that_are_not_family_groups():
    assert extract_family_groups({"name": "Home", "enabled": True}) == {}
