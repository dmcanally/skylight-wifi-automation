from __future__ import annotations

import argparse
import asyncio
import logging
import os

from prometheus_client import start_http_server

from . import __version__
from .clients import SkylightClient
from .config import load_config
from .metrics import Metrics
from .service import AutomationService
from .unifi import UnifiClient


def _secret(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def _silence_http_debug_logs() -> None:
    # aiohttp debug logging prints request headers, including X-API-KEY.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.client").setLevel(logging.WARNING)


async def async_main() -> None:
    parser = argparse.ArgumentParser(description=f"skylight-wifi {__version__}")
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH", "/config/config.yaml"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    unifi = UnifiClient(
        _secret("UNIFI_API_KEY"),
        config.unifi_url,
        config.unifi_tls_ca,
        config.unifi_site,
    )
    try:
        async with SkylightClient(
            _secret("SKYLIGHT_EMAIL"), _secret("SKYLIGHT_PASSWORD")
        ) as skylight:
            service = AutomationService(config, skylight, unifi, Metrics())
            if args.once:
                await service.poll()
            else:
                await service.run_forever()
    finally:
        await unifi.close()


def main() -> None:
    _silence_http_debug_logs()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    start_http_server(8080)
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
