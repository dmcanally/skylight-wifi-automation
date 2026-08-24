from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .clients import GoogleWifiClient, SkylightClient
from .config import AppConfig
from .evaluator import decide

LOGGER = logging.getLogger(__name__)


class AutomationService:
    def __init__(
        self, config: AppConfig, skylight: SkylightClient, google: GoogleWifiClient
    ) -> None:
        self.config = config
        self.skylight = skylight
        self.google = google

    async def run_once(self) -> None:
        now = datetime.now(ZoneInfo(self.config.timezone))
        tasks = await self.skylight.tasks_for(now.date())
        await self.google.refresh()
        for child in self.config.children:
            decision = decide(child, tasks, now, self.config)
            LOGGER.info(
                "child=%s unlock=%s tasks=%d/%d reason=%s dry_run=%s",
                child.name,
                decision.should_unlock,
                decision.complete,
                decision.required,
                decision.reason,
                self.config.dry_run,
            )
            if not child.google_device_ids:
                LOGGER.warning("child=%s has no configured Google device IDs", child.name)
                continue
            await self.google.set_internet(
                child.google_device_ids, decision.should_unlock, self.config.dry_run
            )

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                # Fail closed with respect to mutations: an API failure never changes Wi-Fi state.
                LOGGER.exception("poll failed; leaving current Wi-Fi state unchanged")
            await asyncio.sleep(self.config.poll_seconds)

