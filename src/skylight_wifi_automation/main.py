from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

from .clients import GoogleWifiClient, SkylightClient
from .config import load_config
from .service import AutomationService


def _secret(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


async def async_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH", "/config/config.yaml"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--discover-google", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    google = GoogleWifiClient(_secret("GOOGLE_WIFI_REFRESH_TOKEN"))
    try:
        if args.discover_google:
            await google.refresh()
            print(json.dumps(google.discovery_rows(), indent=2))
            return
        async with SkylightClient(
            _secret("SKYLIGHT_EMAIL"), _secret("SKYLIGHT_PASSWORD")
        ) as skylight:
            service = AutomationService(config, skylight, google)
            if args.once:
                await service.run_once()
            else:
                await service.run_forever()
    finally:
        await google.close()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

