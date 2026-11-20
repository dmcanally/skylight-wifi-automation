from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp

from .tls import build_ssl_context

# Published Network Integration API, documented for 11.0.x.
SITES_PATH = "/sites"

# Object Manager "No Internet" objects are object-oriented network configs.
# Network 11.0.99's published Integration OpenAPI does not list them, and it
# does not let a caller set firewall-policy ``enabled``. Generated firewall
# policies are absent while the object is paused, so they are not the toggle.
# These two paths address the object itself. The same X-API-KEY is sent.
# The app never creates or deletes an object.
OBJECTS_COLLECTION = "object-oriented-network-configs"
OBJECT_RESOURCE = "object-oriented-network-config"


class UniFiError(RuntimeError):
    """Safe to log. It never includes the API key, a MAC, or a client IP."""


class UniFiStatus(Exception):
    """HTTP status from a test double or a caller that already read the code."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"status={status}")


@dataclass
class NetworkObject:
    id: str
    name: str
    enabled: bool
    raw: dict[str, Any]


class UnifiClient:
    """Read and pause or resume Object Manager objects.

    ``enabled`` true is active or resumed, which blocks the child.
    ``enabled`` false is paused, which leaves the child's internet on.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        ca_pem: str,
        site: str | None = None,
        *,
        requester: Any = None,
        page_limit: int = 200,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("unifi_url must be an https URL with a hostname")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._ca_pem = ca_pem
        self._site = site
        self._requester = requester
        self.page_limit = page_limit
        # aiohttp debug logs include request headers, and the API key is one.
        logging.getLogger("aiohttp").setLevel(logging.WARNING)
        logging.getLogger("aiohttp.client").setLevel(logging.WARNING)
        self._site_ref: str | None = None
        self._session: aiohttp.ClientSession | None = None

    def __repr__(self) -> str:
        return f"UnifiClient(base_url={self._base_url!r}, site={self._site!r})"

    async def list_sites(self) -> list[dict[str, Any]]:
        """Read-only Integration API site list. Used by the connection test."""
        return await self._collect(f"{self._base_url}{SITES_PATH}")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def list_objects(self) -> list[NetworkObject]:
        site = await self._resolve_site()
        reference = site.get("internalReference")
        if not isinstance(reference, str) or not reference:
            raise UniFiError("unifi site is missing internalReference")
        self._site_ref = reference
        payload = await self._collect(self._collection_url(reference))
        return [_parse_object(item) for item in payload]

    async def set_blocked(
        self, obj: NetworkObject, blocked: bool, allowed_names: frozenset[str]
    ) -> None:
        """Resume when ``blocked`` is true. Pause when it is false.

        The name must be an exact configured ``object_name``. The object is
        read again immediately before the PUT, only ``enabled`` is changed,
        and a follow-up GET must show that value. A mismatch is not retried.
        """
        if self._site_ref is None:
            raise UniFiError("unifi site is not resolved")
        if obj.name not in allowed_names:
            raise UniFiError("refusing to write an object outside the configured names")
        fresh = await self._read_object(obj.id)
        if fresh.name != obj.name or fresh.name not in allowed_names:
            raise UniFiError("refusing to write an object outside the configured names")
        if fresh.enabled == blocked:
            return
        body = dict(fresh.raw)
        body["enabled"] = blocked
        _debug_object("unifi write", fresh.id, fresh.name, blocked)
        await self._request("PUT", self._object_url(self._site_ref, fresh.id), json_body=body)
        confirmed = await self._read_object(fresh.id)
        if confirmed.enabled != blocked or confirmed.name != fresh.name:
            raise UniFiError("unifi object state did not match after write")

    async def _read_object(self, object_id: str) -> NetworkObject:
        if self._site_ref is None:
            raise UniFiError("unifi site is not resolved")
        payload = await self._request("GET", self._object_url(self._site_ref, object_id))
        obj = _parse_object(_unwrap_object(payload))
        _debug_object("unifi read", obj.id, obj.name, obj.enabled)
        return obj

    async def _resolve_site(self) -> dict[str, Any]:
        sites = await self._collect(f"{self._base_url}{SITES_PATH}")
        if self._site:
            for site in sites:
                if self._site in {site.get("id"), site.get("internalReference"), site.get("name")}:
                    if not isinstance(site, dict):
                        break
                    return site
            raise UniFiError("configured unifi site was not found")
        if len(sites) != 1:
            raise UniFiError("expected exactly one unifi site")
        site = sites[0]
        if not isinstance(site, dict):
            raise UniFiError("unifi site entry is not an object")
        return site

    def _collection_url(self, internal_reference: str) -> str:
        return (
            f"{self._origin}/proxy/network/v2/api/site/"
            f"{quote(internal_reference, safe='')}/{OBJECTS_COLLECTION}"
        )

    def _object_url(self, internal_reference: str, object_id: str) -> str:
        return (
            f"{self._origin}/proxy/network/v2/api/site/"
            f"{quote(internal_reference, safe='')}/{OBJECT_RESOURCE}/"
            f"{quote(object_id, safe='')}"
        )

    async def _collect(self, url: str) -> list[dict[str, Any]]:
        offset = 0
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        while offset < 10000:
            payload = await self._request(
                "GET",
                url,
                params={"offset": offset, "limit": self.page_limit},
            )
            page, total = _page(payload, offset)
            if not page:
                if total is not None and offset < total:
                    raise UniFiError("unifi pagination ended early")
                break
            fresh = 0
            for item in page:
                marker = str(item.get("id") or item.get("_id") or item.get("name") or fresh)
                if marker in seen:
                    continue
                seen.add(marker)
                items.append(item)
                fresh += 1
            if fresh == 0:
                raise UniFiError("unifi pagination repeated a page")
            offset += len(page)
            if total is not None and offset >= total:
                break
            if total is None and len(page) < self.page_limit:
                break
        else:
            raise UniFiError("unifi pagination did not finish")
        return items

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if method not in {"GET", "PUT"}:
            raise UniFiError("unsupported unifi method")
        if self._requester is not None:
            try:
                return await self._requester(method, url, params, json_body)
            except UniFiStatus as exc:
                raise UniFiError(f"unifi {method} failed status={exc.status}") from None
        session = await self._ensure_session()
        headers = {
            "X-API-KEY": self._api_key,
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                allow_redirects=False,
            ) as response:
                if response.status < 200 or response.status >= 300:
                    raise UniFiError(f"unifi {method} failed status={response.status}")
                if response.status == 204:
                    return None
                return await response.json(content_type=None)
        except UniFiError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as exc:
            raise UniFiError(f"unifi {method} failed error_type={type(exc).__name__}") from None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=build_ssl_context(self._ca_pem))
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session


def _page(payload: Any, requested_offset: int) -> tuple[list[dict[str, Any]], int | None]:
    if isinstance(payload, list):
        page: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                raise UniFiError("unifi page entry is not an object")
            page.append(item)
        return page, None
    if not isinstance(payload, dict):
        raise UniFiError("unifi response is not a page")
    if "data" not in payload:
        raise UniFiError("unifi page is missing data")
    data = payload["data"]
    if not isinstance(data, list):
        raise UniFiError("unifi page data is not a list")
    parsed: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise UniFiError("unifi page entry is not an object")
        parsed.append(item)
    if "count" in payload and _require_int(payload["count"], "count") != len(parsed):
        raise UniFiError("unifi pagination count mismatch")
    if "offset" in payload and _require_int(payload["offset"], "offset") != requested_offset:
        raise UniFiError("unifi pagination offset mismatch")
    if "totalCount" not in payload:
        return parsed, None
    return parsed, _require_int(payload["totalCount"], "totalCount")


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UniFiError(f"unifi pagination {label} is not an integer")
    if value < 0:
        raise UniFiError(f"unifi pagination {label} is negative")
    return value


def _unwrap_object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise UniFiError("unifi object response has an unexpected shape")
    data = payload.get("data", payload)
    if isinstance(data, dict) and "enabled" in data:
        return data
    if "enabled" in payload and "data" not in payload:
        return payload
    raise UniFiError("unifi object response has an unexpected shape")


def _debug_object(action: str, object_id: str, name: str, enabled: bool) -> None:
    # Id, name, and enabled only. Never the body, a MAC, or an IP.
    logging.getLogger(__name__).debug(
        "%s id=%s name=%s enabled=%s", action, object_id, name, enabled
    )


def _parse_object(item: dict[str, Any]) -> NetworkObject:
    object_id = item.get("id") or item.get("_id")
    name = item.get("name")
    if not isinstance(object_id, str) or not object_id or not isinstance(name, str) or not name:
        raise UniFiError("unifi object is missing id or name")
    enabled = item.get("enabled")
    if not isinstance(enabled, bool):
        raise UniFiError("unifi object enabled flag is not a boolean")
    return NetworkObject(id=object_id, name=name, enabled=enabled, raw=item)
