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

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def objects_by_name(self) -> dict[str, NetworkObject]:
        site = await self._resolve_site()
        reference = site.get("internalReference")
        if not isinstance(reference, str) or not reference:
            raise UniFiError("unifi site is missing internalReference")
        self._site_ref = reference
        payload = await self._collect(self._collection_url(reference))
        found: dict[str, NetworkObject] = {}
        for item in payload:
            obj = _parse_object(item)
            if obj.name in found:
                raise UniFiError("duplicate object name")
            found[obj.name] = obj
        return found

    async def set_blocked(self, obj: NetworkObject, blocked: bool) -> None:
        """Resume the object when ``blocked`` is true, pause it when false."""
        if self._site_ref is None:
            raise UniFiError("unifi site is not resolved")
        if obj.enabled == blocked:
            return
        body = dict(obj.raw)
        body["enabled"] = blocked
        await self._request("PUT", self._object_url(self._site_ref, obj.id), json_body=body)

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
            page, total = _page(payload)
            if not page:
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
                break
            offset += len(page)
            if total is not None and offset >= total:
                break
            if total is None and len(page) < self.page_limit:
                break
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
            return await self._requester(method, url, params, json_body)
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
                if response.status >= 400:
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


def _page(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    if isinstance(payload, list):
        data = payload
        total = None
    elif isinstance(payload, dict):
        data = payload.get("data", [])
        total = payload.get("totalCount")
    else:
        raise UniFiError("unifi response is not a page")
    if not isinstance(data, list):
        raise UniFiError("unifi page data is not a list")
    page: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise UniFiError("unifi page entry is not an object")
        page.append(item)
    if total is None:
        return page, None
    return page, int(total)


def _parse_object(item: dict[str, Any]) -> NetworkObject:
    object_id = item.get("id") or item.get("_id")
    name = item.get("name")
    if not isinstance(object_id, str) or not object_id or not isinstance(name, str) or not name:
        raise UniFiError("unifi object is missing id or name")
    enabled = item.get("enabled")
    if not isinstance(enabled, bool):
        raise UniFiError("unifi object enabled flag is not a boolean")
    return NetworkObject(id=object_id, name=name, enabled=enabled, raw=item)
