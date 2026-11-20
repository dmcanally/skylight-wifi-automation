from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from textwrap import indent

from argon2 import PasswordHasher
from prometheus_client import CollectorRegistry
from starlette.testclient import TestClient
from tests.conftest import PEM, chicago
from tests.fakes import Skylight, UniFiAPI, network_object

from skylight_wifi_automation.auth import dummy_password_hash
from skylight_wifi_automation.config import AppConfig, load_config
from skylight_wifi_automation.metrics import Metrics
from skylight_wifi_automation.redact import REDACTOR
from skylight_wifi_automation.runtime import Runtime
from skylight_wifi_automation.secrets import ResolvedSecrets
from skylight_wifi_automation.service import AutomationService
from skylight_wifi_automation.store import Store
from skylight_wifi_automation.unifi import UnifiClient
from skylight_wifi_automation.web import AppContext, create_app

FAST = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1)
USERNAME = "admin"
PASSWORD = "long-enough-pw"
ORIGIN = {"origin": "http://testserver"}
SETUP_TOKEN = "setup-token-value"


class Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


@dataclass
class Bundle:
    app: object
    store: Store
    service: AutomationService
    runtime: Runtime
    api: UniFiAPI
    sky: Skylight
    clock: Clock
    yaml_path: Path
    password: str = PASSWORD
    username: str = USERNAME


def write_yaml(
    path: Path,
    *,
    dry_run: bool = False,
    manual: bool = False,
    cutoff: str = "20:30",
    pem: str = PEM,
    extra_children: str = "",
) -> None:
    block = indent(pem.strip() + "\n", "  ")
    manual_text = "true" if manual else "false"
    path.write_text(
        f"""timezone: America/Chicago
poll_seconds: 60
dry_run: {str(dry_run).lower()}
include_late: true
include_routines: true
empty_day_unlocks: false
unifi_url: https://unifi.local/proxy/network/integration/v1
unifi_tls_ca: |
{block}children:
  - name: child-a
    skylight_profile: Child A
    object_name: No Internet - child-a
    cutoff: "{cutoff}"
    manual_unblock: {manual_text}
{extra_children}""",
        encoding="utf-8",
    )


def build(
    tmp_path: Path,
    *,
    source: str = "db",
    setup_token: str | None = SETUP_TOKEN,
    cookie_secure: bool = False,
    key: bytes | None | object = "generate",
    moment: datetime | None = None,
    dry_run: bool = False,
    manual: bool = False,
    cutoff: str = "20:30",
    enabled: bool = True,
) -> Bundle:
    if key == "generate":
        secret_key: bytes | None = os.urandom(32)
    else:
        secret_key = key  # type: ignore[assignment]
    yaml_path = tmp_path / "config.yaml"
    write_yaml(yaml_path, dry_run=dry_run, manual=manual, cutoff=cutoff)
    clock = Clock(moment or chicago(2026, 8, 24, 18))
    store = Store(tmp_path / "app.db", secret_key)
    api = UniFiAPI([network_object(enabled=enabled)])
    sky = Skylight([])

    async def factory(secrets: ResolvedSecrets, config: AppConfig) -> tuple[object, object]:
        unifi = UnifiClient(
            secrets.api_key or "missing-api-key",
            config.unifi_url,
            config.unifi_tls_ca,
            config.unifi_site,
            requester=api,
        )
        return sky, unifi

    async def init() -> tuple[Runtime, AutomationService]:
        runtime = Runtime(store, source, yaml_path, clock, factory, None, None)
        if source == "db":
            await runtime.import_if_needed()
            config = await store.read_config()
        else:
            config = load_config(yaml_path)
        secrets = await store.resolve_secrets()
        runtime.config = config
        runtime.secrets = secrets
        skylight, unifi = await runtime.open_clients()
        service = AutomationService(
            config,
            skylight,
            unifi,
            Metrics(CollectorRegistry()),
            clock=clock,
            latch_path=tmp_path / "latch.json",
            overrides=store if source == "db" else None,
            reloader=runtime.reload_into,
        )
        return runtime, service

    REDACTOR.install()
    runtime, service = asyncio.run(init())
    app = create_app(
        AppContext(
            store=store,
            service=service,
            source=source,
            clock=clock,
            setup_token=setup_token,
            cookie_secure=cookie_secure,
            hasher=FAST,
            dummy_hash=dummy_password_hash(FAST),
            tester=None,
            read_only=source == "file",
        )
    )
    return Bundle(app, store, service, runtime, api, sky, clock, yaml_path)


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, html[:500]
    return match.group(1)


def setup_admin(client: TestClient, token: str = SETUP_TOKEN, password: str = PASSWORD) -> object:
    page = client.get("/setup")
    return client.post(
        "/setup",
        data={
            "csrf_token": csrf_from(page.text),
            "setup_token": token,
            "username": USERNAME,
            "password": password,
            "password2": password,
        },
        headers=ORIGIN,
        follow_redirects=False,
    )


def login(client: TestClient, password: str = PASSWORD, username: str = USERNAME) -> object:
    page = client.get("/login")
    return client.post(
        "/login",
        data={"csrf_token": csrf_from(page.text), "username": username, "password": password},
        headers=ORIGIN,
        follow_redirects=False,
    )


def sign_in(client: TestClient) -> str:
    setup_admin(client)
    response = login(client)
    assert response.status_code == 303
    page = client.get("/")
    assert page.status_code == 200
    return csrf_from(page.text)


def db_rows(path: Path, sql: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute(sql).fetchall())
    finally:
        connection.close()


def poll(bundle: Bundle) -> None:
    asyncio.run(bundle.service.poll())
