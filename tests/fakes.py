from __future__ import annotations

from typing import Any

from tests.conftest import CLIENT_IP, DEVICE_NAME, MAC

from skylight_wifi_automation.unifi import UniFiStatus


def network_object(
    name: str = "No Internet - child-a",
    *,
    object_id: str = "obj-child-a",
    enabled: Any = True,
) -> dict[str, Any]:
    return {
        "id": object_id,
        "name": name,
        "enabled": enabled,
        "targets": [{"type": "MAC", "value": MAC}],
        "hostname": DEVICE_NAME,
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
        http_status: int | None = None,
        http_status_on: str | None = None,
        confirm_mismatch: bool = False,
        fresh_stamp: int | None = None,
        pagination: str | None = None,
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
        self.http_status = http_status
        self.http_status_on = http_status_on
        self.confirm_mismatch = confirm_mismatch
        self.fresh_stamp = fresh_stamp
        self.pagination = pagination
        self.calls: list[dict[str, Any]] = []
        self.singular_gets = 0
        self.put_count = 0
        self.put_enabled: bool | None = None
        self.last_read: dict[str, Any] | None = None

    async def __call__(self, method, url, params, body):
        self.calls.append(
            {"method": method, "url": url, "params": dict(params or {}), "body": body}
        )
        if method not in {"GET", "PUT"}:
            raise AssertionError(method)
        if "firewall" in url:
            raise AssertionError(url)
        status = self._status(method, url)
        if status is not None:
            raise UniFiStatus(status)
        if self.fail == "sites" and url.rstrip("/").endswith("/sites"):
            raise OSError("test-api-key-do-not-log")
        if self.fail == "objects" and url.endswith("object-oriented-network-configs"):
            raise OSError("test-api-key-do-not-log")
        if self.fail == "write" and method == "PUT":
            raise OSError("test-api-key-do-not-log")
        if self.pagination == "offset" and url.endswith("object-oriented-network-configs"):
            return {"offset": 5, "limit": 1, "count": 0, "totalCount": 2, "data": []}
        if url.rstrip("/").endswith("/sites"):
            return _page(self.sites, params)
        if url.endswith("/object-oriented-network-configs"):
            return _page(self.objects, params)
        if method == "GET" and "/object-oriented-network-config/" in url:
            return self._read_one(url)
        if method == "PUT" and "/object-oriented-network-config/" in url:
            return self._write_one(url, body)
        raise AssertionError(url)

    def _status(self, method: str, url: str) -> int | None:
        if self.http_status is None:
            return None
        listing = url.endswith("object-oriented-network-configs")
        singular = "/object-oriented-network-config/" in url
        if self.http_status_on == "list" and method == "GET" and listing:
            return self.http_status
        if self.http_status_on == "read" and method == "GET" and singular:
            return self.http_status
        if self.http_status_on == "put" and method == "PUT":
            return self.http_status
        return None

    def _read_one(self, url: str) -> dict[str, Any]:
        obj = self._find(url)
        self.singular_gets += 1
        payload = dict(obj)
        if self.fresh_stamp is not None and self.singular_gets == 1:
            payload["stamp"] = self.fresh_stamp
        self.last_read = dict(payload)
        if self.confirm_mismatch and self.put_count:
            payload["enabled"] = not self.put_enabled
        return payload

    def _write_one(self, url: str, body: dict[str, Any] | None) -> dict[str, Any]:
        obj = self._find(url)
        if body is None:
            raise AssertionError("missing body")
        baseline = self.last_read if self.last_read is not None else obj
        for key in baseline:
            if key not in body:
                raise AssertionError(key)
        for key, value in body.items():
            if key != "enabled" and baseline.get(key) != value:
                raise AssertionError(key)
        obj["enabled"] = body["enabled"]
        if "stamp" in body:
            obj["stamp"] = body["stamp"]
        self.put_count += 1
        self.put_enabled = body["enabled"]
        return {"id": obj["id"], "enabled": obj["enabled"]}

    def _find(self, url: str) -> dict[str, Any]:
        object_id = url.rsplit("/", 1)[-1]
        for obj in self.objects:
            if obj["id"] == object_id:
                return obj
        raise OSError("missing object")

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
