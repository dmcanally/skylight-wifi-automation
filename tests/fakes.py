from __future__ import annotations

from typing import Any

from tests.conftest import CLIENT_IP, MAC


def network_object(
    name: str = "No Internet - Lanie",
    *,
    object_id: str = "obj-lanie",
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "id": object_id,
        "name": name,
        "enabled": enabled,
        "targets": [{"type": "MAC", "value": MAC}],
        "client_address": CLIENT_IP,
    }


class Skylight:
    def __init__(self, tasks: list | None = None, error: BaseException | None = None) -> None:
        self.tasks = list(tasks or [])
        self.error = error

    async def tasks_for(self, day):
        if self.error:
            raise self.error
        return list(self.tasks)


class UniFiAPI:
    def __init__(
        self,
        objects: list[dict[str, Any]] | None = None,
        *,
        fail: str | None = None,
    ) -> None:
        self.objects = objects if objects is not None else [network_object()]
        self.sites = [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "internalReference": "default",
                "name": "Default",
            }
        ]
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, method, url, params, body):
        self.calls.append(
            {"method": method, "url": url, "params": dict(params or {}), "body": body}
        )
        if method not in {"GET", "PUT"}:
            raise AssertionError(method)
        if "firewall" in url:
            raise AssertionError(url)
        if self.fail == "sites" and url.rstrip("/").endswith("/sites"):
            raise OSError("test-api-key-do-not-log")
        if self.fail == "objects" and url.endswith("object-oriented-network-configs"):
            raise OSError("test-api-key-do-not-log")
        if self.fail == "write" and method == "PUT":
            raise OSError("test-api-key-do-not-log")
        if url.rstrip("/").endswith("/sites"):
            return _page(self.sites, params)
        if url.endswith("/object-oriented-network-configs"):
            return _page(self.objects, params)
        if method == "PUT" and "/object-oriented-network-config/" in url:
            object_id = url.rsplit("/", 1)[-1]
            for obj in self.objects:
                if obj["id"] == object_id:
                    for key, value in body.items():
                        if key != "enabled" and obj.get(key) != value:
                            raise AssertionError(key)
                    obj["enabled"] = body["enabled"]
                    return {"id": obj["id"], "enabled": obj["enabled"]}
            raise OSError("missing object")
        raise AssertionError(url)

    def writes(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["method"] != "GET"]


def _page(items: list[dict[str, Any]], params: dict[str, Any] | None) -> dict[str, Any]:
    params = params or {}
    offset = int(params["offset"])
    limit = int(params["limit"])
    chunk = items[offset : offset + limit]
    return {
        "offset": offset,
        "limit": limit,
        "count": len(chunk),
        "totalCount": len(items),
        "data": [dict(item) for item in chunk],
    }
