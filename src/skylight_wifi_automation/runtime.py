from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from .clients import SkylightClient
from .config import AppConfig, load_config
from .redact import REDACTOR
from .secrets import ResolvedSecrets
from .unifi import UnifiClient

LOGGER = logging.getLogger(__name__)

ClientFactory = Callable[[ResolvedSecrets, AppConfig], Awaitable[tuple[object, object]]]


def database_path() -> Path:
    directory = Path(os.environ.get("DATA_DIR", "/data"))
    if not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK):
        raise RuntimeError(f"DATA_DIR is not writable: {directory}")
    return directory / "app.db"


def config_source() -> str:
    value = os.environ.get("CONFIG_SOURCE", "db").strip().lower()
    if value not in {"db", "file"}:
        raise RuntimeError("CONFIG_SOURCE must be db or file")
    return value


def ui_port() -> int:
    raw = os.environ.get("UI_PORT", "8000")
    try:
        port = int(raw)
    except ValueError:
        raise RuntimeError("UI_PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise RuntimeError("UI_PORT must be an integer")
    return port


def cookie_secure() -> bool:
    return os.environ.get("COOKIE_SECURE", "true").strip().lower() != "false"


def setup_token() -> str | None:
    raw = os.environ.get("ADMIN_SETUP_TOKEN")
    if raw is None or raw.strip() == "":
        return None
    token = raw.strip()
    if len(token) < 32:
        LOGGER.error("ADMIN_SETUP_TOKEN must be at least 32 characters; setup is disabled")
        return None
    return token


def _proxy_token_is_open(token: str) -> bool:
    if token == "*":
        return True
    if "/" not in token:
        return False
    try:
        network = ipaddress.ip_network(token, strict=False)
    except ValueError:
        return False
    return network.prefixlen == 0


def trusted_proxies() -> str:
    raw = os.environ.get("TRUSTED_PROXIES", "").strip()
    if not raw:
        return ""
    for item in raw.split(","):
        token = item.strip()
        if token and _proxy_token_is_open(token):
            raise RuntimeError(
                "TRUSTED_PROXIES must list specific proxy addresses, not *, 0.0.0.0/0, or ::/0"
            )
    return raw


def load_named_key(name: str) -> bytes | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise RuntimeError(f"{name} must be base64-encoded 32 bytes") from None
    if len(key) != 32:
        raise RuntimeError(f"{name} must be base64-encoded 32 bytes")
    return key


def load_encryption_key() -> bytes | None:
    return load_named_key("APP_ENCRYPTION_KEY")


def config_fingerprint(config: AppConfig) -> str:
    payload = config.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def client_fingerprint(secrets: ResolvedSecrets, config: AppConfig) -> str:
    payload = {
        "api_key": secrets.api_key,
        "ca": config.unifi_tls_ca,
        "email": secrets.email,
        "password": secrets.password,
        "site": config.unifi_site or "",
        "url": config.unifi_url,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def ensure_secrets(secrets: ResolvedSecrets, key_present: bool) -> None:
    REDACTOR.update(secrets.materials())
    missing = [name for name, source in secrets.sources.items() if source == "missing"]
    if not missing:
        return
    if not key_present:
        raise RuntimeError(f"Missing required environment variable {missing[0]}")
    for name in missing:
        LOGGER.error("secret not set name=%s; polls stay fail-closed until it is set", name)


class Runtime:
    def __init__(
        self,
        store: object,
        source: str,
        config_path: Path,
        clock: Callable[[], datetime],
        factory: ClientFactory,
        config: AppConfig | None,
        secrets: ResolvedSecrets | None,
    ) -> None:
        self.store = store
        self.source = source
        self.config_path = config_path
        self.clock = clock
        self.factory = factory
        self.config = config
        self.secrets = secrets
        self.version = 0
        self.fingerprint = ""
        self._drift_warned = False

    def __repr__(self) -> str:
        return f"Runtime(source={self.source!r})"

    async def import_if_needed(self) -> None:
        if self.source != "db" or await self.store.is_imported():
            return
        loaded = load_config(self.config_path)
        await self.store.import_config(loaded, self.clock(), config_fingerprint(loaded))

    async def warn_if_drift(self) -> None:
        if self.source != "db" or self._drift_warned:
            return
        self._drift_warned = True
        stored = await self.store.imported_hash()
        if not isinstance(stored, str):
            return
        try:
            loaded = load_config(self.config_path)
        except Exception:  # noqa: BLE001 - do not log the file contents
            LOGGER.warning(
                "config file differs from the database or could not be validated; "
                "database is the source of truth"
            )
            return
        if config_fingerprint(loaded) != stored:
            LOGGER.warning(
                "config file differs from the database; database is the source of truth"
            )

    async def open_clients(self) -> tuple[object, object]:
        REDACTOR.update(self.secrets.materials())
        self.fingerprint = client_fingerprint(self.secrets, self.config)
        self.version = int(await self.store.config_version())
        return await self.factory(self.secrets, self.config)

    async def reload_into(self, service: object) -> None:
        version = int(await self.store.config_version())
        if version == self.version:
            return
        if self.source == "db":
            try:
                config = await self.store.read_config()
            except ValidationError:
                LOGGER.error("database config is invalid; keeping the running snapshot")
                return
        else:
            config = self.config
        secrets = await self.store.resolve_secrets()
        REDACTOR.update(secrets.materials())
        new_fingerprint = client_fingerprint(secrets, config)
        if new_fingerprint != self.fingerprint:
            try:
                skylight, unifi = await self.factory(secrets, config)
            except Exception as exc:  # noqa: BLE001 - rebuild errors may contain a secret
                LOGGER.error("client rebuild failed error_type=%s", type(exc).__name__)
                return
            old_skylight = service.skylight  # type: ignore[attr-defined]
            old_unifi = service.unifi  # type: ignore[attr-defined]
            service.skylight = skylight  # type: ignore[attr-defined]
            service.unifi = unifi  # type: ignore[attr-defined]
            await close_clients(old_skylight, old_unifi)
            self.fingerprint = new_fingerprint
            self.secrets = secrets
        service.config = config  # type: ignore[attr-defined]
        self.config = config
        self.version = version


async def close_clients(skylight: object, unifi: object) -> None:
    close = getattr(unifi, "close", None)
    if close is not None:
        try:
            await close()
        except Exception:  # noqa: BLE001 - close failures are logged without the error text
            LOGGER.warning("unifi client close failed")
    if getattr(skylight, "_client", None) is not None:
        exit_ = getattr(skylight, "__aexit__", None)
        if exit_ is not None:
            try:
                await exit_(None, None, None)
            except Exception:  # noqa: BLE001 - close failures are logged without the error text
                LOGGER.warning("skylight client close failed")


async def test_connections(secrets: ResolvedSecrets, config: AppConfig) -> str:
    """Read-only Skylight login plus frames, and a UniFi site GET."""
    problems: list[str] = []
    if secrets.sources.get("SKYLIGHT_EMAIL") == "missing" or secrets.sources.get("SKYLIGHT_PASSWORD") == "missing":
        problems.append("not_configured")
    else:
        skylight = SkylightClient(secrets.email, secrets.password)
        try:
            await skylight.__aenter__()
            await skylight.ping()
        except Exception as exc:  # noqa: BLE001 - report the error class, not the message
            LOGGER.warning("connection test failed target=skylight error_type=%s", type(exc).__name__)
            problems.append("skylight_error")
        finally:
            await close_clients(skylight, _NullClient())
    if secrets.sources.get("UNIFI_API_KEY") == "missing":
        if "not_configured" not in problems:
            problems.append("not_configured")
    else:
        unifi = UnifiClient(secrets.api_key, config.unifi_url, config.unifi_tls_ca, config.unifi_site)
        try:
            await unifi.list_sites()
        except Exception as exc:  # noqa: BLE001 - report the error class, not the message
            LOGGER.warning("connection test failed target=unifi error_type=%s", type(exc).__name__)
            problems.append("unifi_error")
        finally:
            await unifi.close()
    if not problems:
        return "ok"
    return ",".join(dict.fromkeys(problems))


class _NullClient:
    async def close(self) -> None:
        return None


def utc_now() -> datetime:
    return datetime.now(UTC)
