from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from argon2 import PasswordHasher
from prometheus_client import start_http_server

from . import __version__
from .auth import dummy_password_hash
from .config import AppConfig, load_config
from .metrics import Metrics
from .redact import REDACTOR
from .runtime import (
    Runtime,
    close_clients,
    config_source,
    cookie_secure,
    database_path,
    ensure_secrets,
    load_encryption_key,
    load_named_key,
    setup_token,
    trusted_proxies,
    ui_port,
    utc_now,
)
from .secrets import ResolvedSecrets
from .service import AutomationService
from .store import Store
from .web import AppContext, create_app


async def production_factory(secrets: ResolvedSecrets, config: AppConfig) -> tuple[object, object]:
    from .clients import SkylightClient
    from .unifi import UnifiClient

    skylight = SkylightClient(secrets.email, secrets.password)
    if secrets.email and secrets.password:
        try:
            await skylight.__aenter__()
        except Exception as exc:  # noqa: BLE001 - the client error may contain a secret
            logging.getLogger(__name__).error(
                "skylight client open failed error_type=%s", type(exc).__name__
            )
    unifi = UnifiClient(secrets.api_key, config.unifi_url, config.unifi_tls_ca, config.unifi_site)
    return skylight, unifi


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"skylight-wifi {__version__}")
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH", "/config/config.yaml"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("command", nargs="?", choices=["reset-admin", "rotate-key"])
    return parser.parse_args(argv)


def _fail(exc: RuntimeError) -> None:
    logging.getLogger(__name__).error("%s", exc)
    raise SystemExit(1) from None


async def rotate_key() -> None:
    current = load_encryption_key()
    replacement = load_named_key("APP_ENCRYPTION_KEY_NEW")
    if current is None or replacement is None:
        raise RuntimeError("APP_ENCRYPTION_KEY and APP_ENCRYPTION_KEY_NEW must both be set")
    store = Store(database_path(), current)
    try:
        count = await store.rotate_key(replacement)
    finally:
        store.close()
    logging.getLogger(__name__).info("rotated %s secret(s)", count)


async def reset_admin() -> None:
    store = Store(database_path(), load_encryption_key())
    try:
        removed = await store.delete_admin(datetime.now(UTC))
    finally:
        store.close()
    if removed:
        logging.getLogger(__name__).info("admin reset")
    else:
        logging.getLogger(__name__).info("admin reset found no admin")


async def build_service(args: argparse.Namespace) -> tuple[Store, Runtime, AutomationService]:
    key = load_encryption_key()
    store = Store(database_path(), key)
    source = config_source()
    runtime = Runtime(store, source, Path(args.config), utc_now, production_factory, None, None)
    if source == "db":
        await runtime.import_if_needed()
        config = await store.read_config()
        await runtime.warn_if_drift()
    else:
        config = load_config(args.config)
    secrets = await store.resolve_secrets()
    ensure_secrets(secrets, key is not None)
    runtime.config = config
    runtime.secrets = secrets
    skylight, unifi = await runtime.open_clients()
    service = AutomationService(
        config,
        skylight,
        unifi,
        Metrics(),
        clock=utc_now,
        overrides=store if source == "db" else None,
        reloader=runtime.reload_into,
    )
    return store, runtime, service


async def async_main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        if args.command == "reset-admin":
            await reset_admin()
            return
        if args.command == "rotate-key":
            await rotate_key()
            return
        await _serve(args)
    except RuntimeError as exc:
        _fail(exc)


async def _serve(args: argparse.Namespace) -> None:
    store, runtime, service = await build_service(args)
    if args.once:
        try:
            await service.poll()
        finally:
            await close_clients(service.skylight, service.unifi)
            store.close()
        return
    hasher = PasswordHasher()
    app = create_app(
        AppContext(
            store=store,
            service=service,
            source=runtime.source,
            clock=utc_now,
            setup_token=setup_token(),
            cookie_secure=cookie_secure(),
            hasher=hasher,
            dummy_hash=dummy_password_hash(hasher),
            tester=None,
            read_only=runtime.source == "file",
            trusted_proxies=trusted_proxies(),
        )
    )
    proxies = trusted_proxies()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=ui_port(),
            access_log=False,
            log_config=None,
            proxy_headers=True,
            forwarded_allow_ips=proxies,
        )
    )
    await asyncio.gather(server.serve(), service.run_forever())


def _silence_http_debug_logs() -> None:
    # aiohttp debug logging prints request headers, including X-API-KEY.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.client").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    _silence_http_debug_logs()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    REDACTOR.install()
    args = parse_args(argv)
    if args.command == "reset-admin":
        try:
            asyncio.run(reset_admin())
        except RuntimeError as exc:
            _fail(exc)
        return
    if args.command == "rotate-key":
        try:
            asyncio.run(rotate_key())
        except RuntimeError as exc:
            _fail(exc)
        return
    start_http_server(8080)
    asyncio.run(async_main(argv))


if __name__ == "__main__":
    main()
