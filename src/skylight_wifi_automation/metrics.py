from __future__ import annotations

import time

from prometheus_client import REGISTRY, Counter
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import CollectorRegistry


class _LastSuccess:
    """Gauge that stays absent until a poll finishes without error.

    Exporting 0 at startup would look like a success in 1970 and would
    trip WifiAutomationLastSuccessStale before the first poll.
    """

    def __init__(self) -> None:
        self.value: float | None = None

    def set(self, value: float) -> None:
        self.value = value

    def collect(self):
        if self.value is None:
            return
        metric = GaugeMetricFamily(
            "wifi_automation_last_success_timestamp_seconds",
            "Unix seconds when the last poll finished without error.",
        )
        metric.add_metric([], self.value)
        yield metric


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self._registry = registry if registry is not None else REGISTRY
        self.failures = Counter(
            "wifi_automation_poll_failures_total",
            "Polls that failed because Skylight or UniFi returned an error.",
            registry=self._registry,
        )
        self._last_success = _LastSuccess()
        self._registry.register(self._last_success)

    def record_failure(self) -> None:
        self.failures.inc()

    def record_success(self) -> None:
        self._last_success.set(time.time())
